"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, Conversation, ConversationDetail, downloadUrl, ModelInfo, Preview, Run, streamRun, Usage } from "../../lib/api";
import { formatTokens, isTerminal, timeAgo } from "../../lib/format";
import Composer from "./composer";
import PreviewPane from "./preview-pane";
import Turn, { Item, reduceEvent } from "./turn";

const SUGGESTIONS = [
  { kind: "聊天", text: "用通俗的话解释一下什么是大模型的上下文窗口" },
  { kind: "演示文稿", text: "帮我做一个 PPT，介绍城市老旧小区改造的背景、做法和成效，8 页左右" },
  { kind: "表格", text: "做一个 Excel 表格：三个区域 7～9 月的销售额，带合计、环比和柱状图" },
  { kind: "写作", text: "帮我写一份周报模板，适合技术团队使用" },
];

function useModels() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModelState] = useState("");
  useEffect(() => {
    // The platform decides which models exist; the user only picks one (their last choice is remembered).
    api<{ default: string | null; models: ModelInfo[] }>("/v1/models")
      .then((res) => {
        setModels(res.models);
        const remembered = localStorage.getItem("ailab.model");
        setModelState(res.models.some((m) => m.id === remembered) ? remembered! : res.default || res.models[0]?.id || "");
      })
      .catch(() => setModels([]));
  }, []);
  const setModel = (id: string) => {
    setModelState(id);
    localStorage.setItem("ailab.model", id);
  };
  return { models, model, setModel };
}

function Sidebar({ conversations, current, onDelete }: { conversations: Conversation[]; current: string | null; onDelete: (c: Conversation) => void }) {
  const [usage, setUsage] = useState<Usage | null>(null);
  useEffect(() => {
    const load = () => api<Usage>("/v1/usage").then(setUsage).catch(() => setUsage(null));
    load();
    const timer = setInterval(load, 30000);
    return () => clearInterval(timer);
  }, []);
  const pct = usage ? Math.min(100, Math.round((usage.daily_tokens.used / Math.max(1, usage.daily_tokens.limit)) * 100)) : 0;
  return (
    <aside className="conv-sidebar">
      <a className="btn new-chat" href="/">
        ＋ 新对话
      </a>
      <nav className="conv-list" aria-label="对话列表">
        {conversations.length === 0 ? <div className="hint conv-empty">还没有对话</div> : null}
        {conversations.map((c) => (
          <div key={c.id} className={`conv-item ${c.id === current ? "on" : ""}`}>
            <a href={`/c/${c.id}`} title={c.title}>
              <span className="conv-title">{c.title || "新对话"}</span>
              <span className="conv-time">
                {c.last_status === "running" || c.last_status === "queued" ? "回答中…" : timeAgo(c.updated_at)}
              </span>
            </a>
            <button className="link conv-del" aria-label={`删除 ${c.title}`} onClick={() => onDelete(c)}>
              ✕
            </button>
          </div>
        ))}
      </nav>
      {usage ? (
        <div className="usage-mini" title="Token 按模型倍率折算">
          <div className="meter-head">
            <span>今日额度已用 {pct}%</span>
            <span>
              {formatTokens(usage.daily_tokens.used)} / {formatTokens(usage.daily_tokens.limit)}
            </span>
          </div>
          <div className="meter-bar">
            <div className={`meter-fill ${pct >= 100 ? "full" : pct >= 80 ? "warn" : ""}`} style={{ width: `${pct}%` }} />
          </div>
        </div>
      ) : null}
    </aside>
  );
}

export default function Chat({ id }: { id: string | null }) {
  const router = useRouter();
  const { models, model, setModel } = useModels();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [items, setItems] = useState<Record<string, Item[]>>({}); // run id -> live items (runs streamed in this page)
  const [livePreviews, setLivePreviews] = useState<Preview[]>([]);
  const [paneOpen, setPaneOpen] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [streaming, setStreaming] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);

  const loadList = useCallback(() => api<Conversation[]>("/v1/conversations").then(setConversations).catch(() => undefined), []);
  const loadDetail = useCallback(async () => {
    if (!id) return null;
    try {
      const d = await api<ConversationDetail>(`/v1/conversations/${id}`);
      setDetail(d);
      return d;
    } catch (err: any) {
      setError(String(err?.message || err));
      return null;
    }
  }, [id]);

  const follow = useCallback(
    (run: Run) => {
      const controller = new AbortController();
      setStreaming(run.id);
      setItems((prev) => ({ ...prev, [run.id]: prev[run.id] || [] }));
      streamRun(
        run.id,
        (event) => {
          setItems((prev) => ({ ...prev, [run.id]: reduceEvent(prev[run.id] || [], event) }));
          if (event.type === "preview.updated") {
            const p: Preview = { ...event.payload, run_id: run.id };
            setLivePreviews((prev) => [...prev.filter((x) => x.filename !== p.filename), p]);
            setSelected(p.filename);
            setPaneOpen(true);
          }
          if (["artifact.created", "run.completed", "run.failed", "run.canceled"].includes(event.type)) loadDetail();
        },
        controller.signal,
        () => {
          setStreaming(null);
          loadDetail();
          loadList();
        }
      ).catch(() => setStreaming(null));
      return controller;
    },
    [loadDetail, loadList]
  );

  useEffect(() => {
    loadList();
  }, [loadList]);

  // open a conversation: load it, and follow its last turn if that is still being answered
  useEffect(() => {
    if (!id) return;
    let controller: AbortController | null = null;
    loadDetail().then((d) => {
      const last = d?.turns[d.turns.length - 1];
      if (last && !isTerminal(last.status)) controller = follow(last);
    });
    return () => controller?.abort();
  }, [id, loadDetail, follow]);

  // keep the newest message in view while it streams, unless the user scrolled up to read
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items, detail]);

  const previews = useMemo(() => {
    const byFile = new Map<string, Preview>();
    for (const p of [...(detail?.previews || []), ...livePreviews]) {
      byFile.delete(p.filename);
      byFile.set(p.filename, p);
    }
    return [...byFile.values()].filter((p) => p.kind);
  }, [detail, livePreviews]);

  const downloads = useMemo(() => {
    const out: Record<string, string> = {};
    for (const t of detail?.turns || []) for (const a of t.artifacts || []) out[a.filename] = downloadUrl(a.download_url);
    return out;
  }, [detail]);

  const modelName = (m: string) => models.find((x) => x.id === m)?.name || m;
  const turns = detail?.turns || [];
  const running = turns.some((t) => !isTerminal(t.status));

  async function send(prompt: string, fileIds: string[]): Promise<boolean> {
    setBusy(true);
    setError("");
    try {
      let convId = id;
      if (!convId) convId = (await api<Conversation>("/v1/conversations", { method: "POST", body: JSON.stringify({ model }) })).id;
      const run = await api<Run>(`/v1/conversations/${convId}/messages`, {
        method: "POST",
        body: JSON.stringify({ prompt, model: model || undefined, file_ids: fileIds }),
      });
      if (!id) {
        router.push(`/c/${convId}`);
        return true;
      }
      setDetail((d) => (d ? { ...d, turns: [...d.turns, run] } : d));
      stick.current = true;
      follow(run);
      loadList();
      return true;
    } catch (err: any) {
      setError(String(err?.message || err).replace(/^409 .*/, "上一条消息还在回答中，请稍候或先停止它"));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    const active = turns.find((t) => !isTerminal(t.status));
    if (active) await api(`/v1/runs/${active.id}/cancel`, { method: "POST" }).catch(() => undefined);
    loadDetail();
  }

  async function remove(c: Conversation) {
    if (!confirm(`删除对话「${c.title || "新对话"}」？（产出的文件仍保留在资料库）`)) return;
    await api(`/v1/conversations/${c.id}`, { method: "DELETE" }).catch(() => undefined);
    if (c.id === id) router.push("/");
    else loadList();
  }

  const showPane = paneOpen && previews.length > 0;
  const composer = (
    <Composer
      models={models}
      model={model}
      onModel={setModel}
      busy={busy}
      running={running}
      onSend={send}
      onStop={stop}
      autoFocus
      placeholder={id ? "继续对话，比如「第三页换成图表」「再加一列占比」" : undefined}
    />
  );

  return (
    <div className={`chat-layout ${showPane ? "with-preview" : ""}`}>
      <Sidebar conversations={conversations} current={id} onDelete={remove} />

      <section className="chat-main">
        {!id ? (
          <div className="chat-home">
            <h1>今天想让 AILab 做点什么？</h1>
            <p className="muted">直接提问，或者让它在隔离沙箱里帮你做 PPT、Excel、处理文件。做出来的文件会在右侧实时预览，可以接着对话修改。</p>
            {composer}
            {error ? <div className="alert error">{error}</div> : null}
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s.text} className="suggestion secondary" disabled={busy || !model} onClick={() => send(s.text, [])}>
                  <small>{s.kind}</small>
                  {s.text}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            <div
              className="messages"
              ref={scroller}
              onScroll={(e) => {
                const el = e.currentTarget;
                stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
              }}
            >
              <div className="messages-inner">
                {detail === null && !error ? <div className="hint">加载中…</div> : null}
                {turns.map((t) => (
                  <Turn
                    key={t.id}
                    run={t}
                    items={items[t.id]}
                    live={streaming === t.id}
                    modelName={modelName}
                    previewable={new Set(previews.map((p) => p.filename))}
                    onPreview={(f) => {
                      setSelected(f);
                      setPaneOpen(true);
                    }}
                  />
                ))}
              </div>
            </div>
            <div className="composer-dock">
              {error ? <div className="alert error">{error}</div> : null}
              {!paneOpen && previews.length ? (
                <button className="link" onClick={() => setPaneOpen(true)}>
                  打开预览（{previews.length}）
                </button>
              ) : null}
              {composer}
            </div>
          </>
        )}
      </section>

      {showPane ? (
        <PreviewPane
          previews={previews}
          selected={selected}
          onSelect={setSelected}
          onClose={() => setPaneOpen(false)}
          live={!!streaming && running}
          downloads={downloads}
        />
      ) : null}
    </div>
  );
}
