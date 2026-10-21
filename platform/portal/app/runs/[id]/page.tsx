"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import {
  api,
  downloadUrl,
  EventItem,
  formatBytes,
  Run,
  streamRun,
} from "../../../lib/api";

function eventDetail(event: EventItem): string {
  const p = event.payload || {};
  switch (event.type) {
    case "run.created":
      return `status=${p.status} model=${p.model}`;
    case "run.status":
      return `status=${p.status}`;
    case "run.completed":
      return p.summary || "完成";
    case "run.failed":
    case "agent.error":
      return p.error || "失败";
    case "agent.started":
      return `model=${p.model}`;
    case "agent.message":
      return p.content;
    case "agent.tool_call":
      return `${p.name} ${JSON.stringify(p.arguments || {}).slice(0, 240)}`;
    case "agent.tool_result": {
      const parts = [p.ok ? "ok" : "failed"];
      if (p.exit_code !== undefined && p.exit_code !== null) parts.push(`exit=${p.exit_code}`);
      if (p.error) parts.push(p.error);
      if (p.stdout) parts.push(String(p.stdout).slice(0, 300));
      return parts.join(" · ");
    }
    case "artifact.created":
      return `${p.filename} · ${formatBytes(p.size || 0)}`;
    case "sandbox.leased":
      return `${p.provider} · ${p.lease_id}`;
    case "sandbox.released":
      return p.reason || "released";
    default:
      return JSON.stringify(p).slice(0, 300);
  }
}

export default function RunPage() {
  const params = useParams<{ id: string }>();
  const id = String(params?.id || "");
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [error, setError] = useState("");
  const [streaming, setStreaming] = useState(true);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Several events can each trigger a reload while earlier ones are still in flight. Only the most recently
  // issued reload may update the page: otherwise a slow "running" response arriving after the "succeeded"
  // one would put the stale status back, and nothing would refresh it once the stream has ended.
  const loadSeq = useRef(0);
  const loadRun = useCallback(async () => {
    const mine = ++loadSeq.current;
    try {
      const fresh = await api<Run>(`/v1/runs/${id}`);
      if (mine === loadSeq.current) setRun(fresh);
    } catch (err: any) {
      setError(String(err?.message || err));
    }
  }, [id]);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    const controller = new AbortController();
    loadRun();
    streamRun(
      id,
      (event) => {
        if (cancelled) return;
        setEvents((prev) => (prev.some((e) => e.seq === event.seq) ? prev : [...prev, event]));
        if (
          ["run.status", "artifact.created", "run.completed", "run.failed", "run.canceled"].includes(
            event.type
          )
        ) {
          loadRun();
        }
      },
      controller.signal,
      () => setStreaming(false)
    ).catch((err) => {
      if (!cancelled) {
        setError(String(err?.message || err));
        setStreaming(false);
      }
    });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [id, loadRun]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "nearest" });
  }, [events.length]);

  async function cancel() {
    try {
      await api(`/v1/runs/${id}/cancel`, { method: "POST" });
      await loadRun();
    } catch (err: any) {
      setError(String(err?.message || err));
    }
  }

  const terminal = run && ["succeeded", "failed", "canceled"].includes(run.status);

  return (
    <main className="container">
      <div className="row" style={{ marginBottom: 18 }}>
        <a href="/">← 返回工作台</a>
        <div style={{ flex: 1 }} />
        {run ? <span className={`status ${run.status}`}>{run.status}</span> : null}
        {run && !terminal ? (
          <button className="danger" onClick={cancel}>
            取消
          </button>
        ) : null}
      </div>

      <h1>{run?.title || "任务详情"}</h1>
      <div className="kv">
        <span>run_id: {id}</span>
        <span>model: {run?.model || "-"}</span>
        <span>stream: {streaming ? "live" : "closed"}</span>
        {run?.usage ? (
          <span>
            tokens: {run.usage.prompt_tokens || 0} + {run.usage.completion_tokens || 0}
          </span>
        ) : null}
      </div>

      <section className="panel" style={{ marginTop: 18 }}>
        <h2>请求</h2>
        <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.7 }}>{run?.prompt || "…"}</div>
      </section>

      {run?.summary ? (
        <section className="panel">
          <h2>结果</h2>
          <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.7 }}>{run.summary}</div>
        </section>
      ) : null}

      {run?.error ? (
        <section className="panel">
          <h2 style={{ color: "var(--danger)" }}>错误</h2>
          <div className="error">{run.error}</div>
        </section>
      ) : null}

      {run?.artifacts && run.artifacts.length > 0 ? (
        <section className="panel">
          <h2>产出</h2>
          <div className="artifacts">
            {run.artifacts.map((artifact) => (
              <div className="artifact" key={artifact.id}>
                <div className="name">{artifact.filename}</div>
                <div className="size">
                  {formatBytes(artifact.size)} · {artifact.content_type}
                </div>
                <a href={downloadUrl(artifact.download_url)} target="_blank" rel="noreferrer">
                  下载
                </a>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <section className="panel">
        <div className="row" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>执行时间线</h2>
          <div style={{ flex: 1 }} />
          <span className="sub" style={{ margin: 0, fontSize: 12 }}>
            {events.length} events
          </span>
        </div>
        {events.length === 0 ? (
          <div className="empty">等待事件…</div>
        ) : (
          <div className="timeline">
            {events.map((event) => (
              <div
                key={event.seq}
                className={`event ${event.type.replaceAll(".", "_")} ${
                  event.type === "agent.tool_result" ? "tool_result" : ""
                }`}
              >
                <span className="type">
                  #{event.seq} {event.type}
                </span>
                <div className="payload">{eventDetail(event)}</div>
                {event.type === "agent.tool_result" && (event.payload?.stdout || event.payload?.stderr) ? (
                  <details>
                    <summary style={{ cursor: "pointer", color: "var(--muted)", fontSize: 12 }}>
                      查看输出
                    </summary>
                    {event.payload?.stdout ? <pre className="log">{event.payload.stdout}</pre> : null}
                    {event.payload?.stderr ? <pre className="log">{event.payload.stderr}</pre> : null}
                  </details>
                ) : null}
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        )}
        {error ? <div className="error">{error}</div> : null}
      </section>
    </main>
  );
}
