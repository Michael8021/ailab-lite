"use client";

import { useState } from "react";
import { api, downloadUrl, EventItem, Run } from "../../lib/api";
import { formatBytes, isTerminal } from "../../lib/format";
import { IconDownload, IconFile } from "../icons";
import Markdown from "../markdown";

export type Step = { name: string; label: string; detail?: string; status: "running" | "ok" | "failed" | "note"; output?: string };
export type Item =
  | { kind: "text"; step: number; text: string }
  | { kind: "thinking"; step: number; text: string; seconds: number; done: boolean }
  | { kind: "steps"; steps: Step[] };

const PREVIEWABLE = /\.(pptx|xlsx|docx|pdf)$/i;

function describeTool(name: string, args: any): { label: string; detail?: string } {
  switch (name) {
    case "load_skill":
      return { label: `读取技能 ${args?.name || ""}` };
    case "search_images":
      return { label: "搜索图片", detail: args?.query };
    case "exec":
      return { label: "运行命令", detail: String(args?.cmd || "").slice(0, 160) };
    case "write_file":
      return { label: "写入文件", detail: args?.path };
    case "read_file":
      return { label: "读取文件", detail: args?.path };
    case "list_files":
      return { label: "查看目录", detail: args?.path };
    case "deliver_file":
      return { label: "交付文件", detail: args?.filename || String(args?.path || "").split("/").pop() };
    default:
      return { label: name };
  }
}

function lastSteps(items: Item[]): Step[] {
  const last = items[items.length - 1];
  if (last && last.kind === "steps") return last.steps;
  const group: Item = { kind: "steps", steps: [] };
  items.push(group);
  return group.steps;
}

/** Fold one event of a run into its display items (text segments interleaved with groups of steps). */
export function reduceEvent(items: Item[], event: EventItem): Item[] {
  const p = event.payload || {};
  const next = items.map((it) => (it.kind === "steps" ? { kind: "steps" as const, steps: [...it.steps] } : { ...it }));
  // anything else the model does means it has stopped thinking
  if (event.type !== "agent.thinking")
    for (const it of next) {
      if (it.kind === "thinking") it.done = true;
      if (it.kind === "steps") for (const s of it.steps) if (s.name === "thinking" && s.status === "running") s.status = "note";
    }
  const note = (label: string, detail?: string, status: Step["status"] = "note") => lastSteps(next).push({ name: event.type, label, detail, status });
  switch (event.type) {
    case "agent.thinking": {
      const last = next[next.length - 1];
      if (last && last.kind === "steps") {
        // thinking between tool calls belongs to the work: one entry in the steps, not a block of its own
        const prev = last.steps[last.steps.length - 1];
        if (prev && prev.name === "thinking" && prev.status === "running") prev.output = (prev.output || "") + (p.text || "");
        else last.steps.push({ name: "thinking", label: "思考", status: "running", output: p.text || "" });
        last.steps[last.steps.length - 1].detail = `${Math.max(1, Math.round(p.seconds || 0))} 秒`;
        break;
      }
      if (last && last.kind === "thinking" && last.step === p.step) {
        last.text += p.text || "";
        last.seconds = p.seconds ?? last.seconds;
      } else next.push({ kind: "thinking", step: p.step, text: p.text || "", seconds: p.seconds || 0, done: false });
      break;
    }
    case "agent.delta": {
      const last = next[next.length - 1];
      if (last && last.kind === "text" && last.step === p.step) last.text += p.text || "";
      else next.push({ kind: "text", step: p.step, text: p.text || "" });
      break;
    }
    case "agent.message": {
      const existing = [...next].reverse().find((it) => it.kind === "text" && it.step === p.step) as Extract<Item, { kind: "text" }> | undefined;
      if (existing) existing.text = p.content || "";
      else next.push({ kind: "text", step: p.step, text: p.content || "" });
      break;
    }
    case "agent.tool_call": {
      const d = describeTool(p.name, p.arguments);
      lastSteps(next).push({ name: p.name, ...d, status: "running" });
      break;
    }
    case "agent.tool_result": {
      for (let i = next.length - 1; i >= 0; i--) {
        const it = next[i];
        if (it.kind !== "steps") continue;
        const step = [...it.steps].reverse().find((s) => s.name === p.name && s.status === "running");
        if (step) {
          step.status = p.ok ? "ok" : "failed";
          step.output = [p.error, p.stdout, p.stderr].filter(Boolean).join("\n").slice(0, 1500);
          break;
        }
      }
      break;
    }
    case "agent.sandbox":
      note("沙箱已就绪");
      break;
    case "agent.waiting_sandbox":
      note("等待空闲沙箱…");
      break;
    case "preview.updated":
      note("预览已更新", p.filename, "ok");
      break;
    case "preview.failed":
      note("预览生成失败", [p.filename, p.error].filter(Boolean).join("：") || undefined, "failed");
      break;
    case "agent.warning":
      note("提示", p.message, "failed");
      break;
    case "agent.autodeliver":
      note("自动归档", p.filename);
      break;
    default:
      break;
  }
  return next;
}

function Thinking({ item, live }: { item: Extract<Item, { kind: "thinking" }>; live: boolean }) {
  const [open, setOpen] = useState<boolean | null>(null);
  const thinking = live && !item.done;
  const shown = open ?? thinking; // open while it thinks, folded afterwards (click to read it again)
  const seconds = Math.max(1, Math.round(item.seconds));
  return (
    <div className={`thinking ${shown ? "open" : ""}`}>
      <button className="thinking-head" onClick={() => setOpen(!shown)} aria-expanded={shown}>
        <span className={`think-icon ${thinking ? "spin" : ""}`}>✦</span>
        {thinking ? "思考中…" : `已深度思考（用时 ${seconds} 秒）`}
        <span className="chev">{shown ? "▾" : "▸"}</span>
      </button>
      {shown ? <div className="thinking-body">{item.text}</div> : null}
    </div>
  );
}

function Steps({ steps, open: openByDefault }: { steps: Step[]; open: boolean }) {
  const [open, setOpen] = useState<boolean | null>(null);
  const shown = open ?? openByDefault;
  const running = steps.some((s) => s.status === "running");
  const work = steps.filter((s) => s.status !== "note").length;
  return (
    <div className={`steps ${shown ? "open" : ""}`}>
      <button className="steps-head" onClick={() => setOpen(!shown)} aria-expanded={shown}>
        <span className={`dot ${running ? "spin" : "done"}`} />
        {running ? steps.filter((s) => s.status === "running").map((s) => s.label).join("、") + "…" : `执行了 ${work || steps.length} 个步骤`}
        <span className="chev">{shown ? "▾" : "▸"}</span>
      </button>
      {shown ? (
        <ol className="steps-list">
          {steps.map((s, i) => (
            <li key={i} className={`step ${s.status}`}>
              <span className="step-mark" />
              <div className="step-body">
                <span className="step-label">{s.label}</span>
                {s.detail ? <span className="step-detail">{s.detail}</span> : null}
                {s.output && (s.status !== "running" || s.name === "thinking") ? (
                  <details>
                    <summary>{s.name === "thinking" ? "思考内容" : "输出"}</summary>
                    <pre className="log">{s.output}</pre>
                  </details>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

export default function Turn({
  run,
  items,
  live,
  modelName,
  previewKey,
  onPreview,
}: {
  run: Run;
  items: Item[] | undefined;
  live: boolean;
  modelName: (id: string) => string;
  previewKey: (run: Run, filename: string) => string | null;
  onPreview: (filename: string) => void;
}) {
  const [history, setHistory] = useState<Item[] | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const shown = items ?? history;
  const terminal = isTerminal(run.status);
  const textShown = shown?.some((it) => it.kind === "text" && it.text.trim());

  async function loadHistory() {
    setLoadingHistory(true);
    try {
      const events = await api<EventItem[]>(`/v1/runs/${run.id}/events/history`);
      setHistory(events.reduce(reduceEvent, [] as Item[]));
    } finally {
      setLoadingHistory(false);
    }
  }

  return (
    <div className="turn">
      <div className="msg user">
        <div className="bubble">{run.prompt}</div>
        {run.input_files?.length ? (
          <div className="attach-row">
            {run.input_files.map((f) => (
              <span key={f.file_id} className="attach-chip">
                <IconFile size={13} /> {f.filename}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      <div className="msg assistant">
        <div className="avatar-ai">A</div>
        <div className="assistant-body">
          {shown?.map((it, i) =>
            it.kind === "text" ? (
              it.text.trim() ? <Markdown key={i} text={it.text} /> : null
            ) : it.kind === "thinking" ? (
              it.text.trim() ? <Thinking key={i} item={it} live={live} /> : null
            ) : (
              <Steps key={i} steps={it.steps} open={live && i === shown.length - 1} />
            )
          )}
          {!shown && run.result?.thinking?.text ? (
            <Thinking item={{ kind: "thinking", step: 0, text: run.result.thinking.text, seconds: run.result.thinking.seconds || 0, done: true }} live={false} />
          ) : null}
          {/* a finished turn whose streamed text has not arrived (or was never streamed here) shows its stored answer */}
          {(!shown || (terminal && !textShown)) && run.summary ? <Markdown text={run.summary} /> : null}
          {live && !textShown && !shown?.some((it) => it.kind !== "text") ? (
            <div className="typing" aria-label="思考中">
              <span />
              <span />
              <span />
            </div>
          ) : null}
          {run.status === "failed" && run.error ? <div className="alert error">出错了：{run.error}</div> : null}
          {run.status === "canceled" ? <div className="hint">已停止</div> : null}

          {run.artifacts?.length ? (
            <div className="files">
              {run.artifacts.map((a) => (
                <div key={a.id} className="file-card">
                  <span className="icon">
                    <IconFile size={18} />
                  </span>
                  <div className="grow">
                    <div className="name" title={a.filename}>
                      {a.filename}
                    </div>
                    <div className="meta">{formatBytes(a.size)}</div>
                  </div>
                  {PREVIEWABLE.test(a.filename) && previewKey(run, a.filename) ? (
                    <button className="secondary sm" onClick={() => onPreview(previewKey(run, a.filename)!)}>
                      预览
                    </button>
                  ) : null}
                  <a className="btn secondary sm" href={downloadUrl(a.download_url)} target="_blank" rel="noreferrer" aria-label={`下载 ${a.filename}`}>
                    <IconDownload size={15} /> 下载
                  </a>
                </div>
              ))}
            </div>
          ) : null}

          {terminal ? (
            <div className="turn-meta">
              <span>{modelName(run.model)}</span>
              {run.usage?.prompt_tokens ? <span>{(run.usage.prompt_tokens || 0) + (run.usage.completion_tokens || 0)} tokens</span> : null}
              {!items && !history ? (
                <button className="link" onClick={loadHistory} disabled={loadingHistory}>
                  {loadingHistory ? "加载中…" : "执行过程"}
                </button>
              ) : null}
              <a href={`/runs/${run.id}`}>详情</a>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
