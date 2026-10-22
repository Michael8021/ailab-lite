"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE, api, FileItem, ModelInfo, redirectIfUnauthorized, Skill } from "../../lib/api";
import { IconChart, IconFile, IconSend, IconSheet, IconSlides, IconSparkles } from "../icons";

type Attachment = { id: string; filename: string; status: string };
export type Mode = "chat" | "work";
export type SendOptions = { mode: Mode; skill?: string };

type SkillButton = { key: string; skill: string; label: string; Icon: (p: { size?: number }) => React.ReactElement; placeholder: string };

// What the skill buttons ask for. `skill` is the platform skill that gets preloaded for the model.
const SKILLS: SkillButton[] = [
  { key: "ppt", skill: "pptx", label: "PPT 生成", Icon: IconSlides, placeholder: "说说主题、页数和给谁看，比如：给新员工介绍公司的 10 页 PPT" },
  { key: "excel", skill: "xlsx", label: "Excel 表格", Icon: IconSheet, placeholder: "说说表里要有什么，比如：三个区域 7～9 月销售额，带合计和柱状图" },
  { key: "data", skill: "xlsx", label: "数据分析", Icon: IconChart, placeholder: "上传数据文件（Excel / CSV），说说想分析什么、要什么图表" },
];

const MODE_HINT: Record<Mode, string> = {
  chat: "只用文字回答，不启动沙箱，更快更省",
  work: "需要时可以做文件、跑代码（启动沙箱）",
};

export default function Composer({
  models,
  model,
  onModel,
  busy,
  running,
  onSend,
  onStop,
  autoFocus,
  placeholder,
}: {
  models: ModelInfo[];
  model: string;
  onModel: (id: string) => void;
  busy: boolean;
  running: boolean;
  onSend: (prompt: string, fileIds: string[], options: SendOptions) => Promise<boolean>;
  onStop: () => void;
  autoFocus?: boolean;
  placeholder?: string;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [mode, setModeState] = useState<Mode>("work");
  const [picked, setPicked] = useState<SkillButton | null>(null);
  const area = useRef<HTMLTextAreaElement | null>(null);

  const [enabled, setEnabled] = useState<Set<string> | null>(null); // skills switched on in the 技能 page
  const [mine, setMine] = useState<Skill[]>([]); // the user's own skills (personal, switched on)

  useEffect(() => {
    if (localStorage.getItem("ailab.mode") === "chat") setModeState("chat");
    api<Skill[]>("/v1/skills")
      .then((list) => {
        setEnabled(new Set(list.filter((s) => s.enabled).map((s) => s.name)));
        setMine(list.filter((s) => s.scope === "personal" && s.enabled));
      })
      .catch(() => setEnabled(null));
  }, []);

  function setMode(next: Mode) {
    setModeState(next);
    localStorage.setItem("ailab.mode", next);
    if (next === "chat") setPicked(null); // a skill makes files: that is work
  }

  function pick(s: SkillButton) {
    setPicked(picked?.key === s.key ? null : s);
    if (mode === "chat") setMode("work");
    area.current?.focus();
  }
  const picker = useRef<HTMLInputElement | null>(null);

  // grow with the text, up to a limit
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(220, el.scrollHeight)}px`;
  }, [text]);

  // uploads are quarantined until the Worker has scanned them
  useEffect(() => {
    if (!files.some((f) => f.status === "pending")) return;
    let stale = false;
    const timer = setInterval(async () => {
      const fresh = await Promise.all(
        files.map((f) => (f.status === "pending" ? api<FileItem>(`/v1/files/${f.id}`).then((x) => ({ ...f, status: x.status })).catch(() => f) : f))
      );
      if (!stale) setFiles((current) => current.map((f) => fresh.find((x) => x.id === f.id) || f));
    }, 1500);
    return () => {
      stale = true;
      clearInterval(timer);
    };
  }, [files]);

  async function upload(list: FileList) {
    setError("");
    setUploading(true);
    try {
      for (const file of Array.from(list)) {
        const form = new FormData();
        form.append("upload", file);
        const resp = await fetch(`${API_BASE}/v1/files`, { method: "POST", body: form });
        redirectIfUnauthorized(resp);
        if (!resp.ok) {
          setError(`${file.name}：上传失败（${resp.status}）`);
          continue;
        }
        const item: FileItem = await resp.json();
        setFiles((prev) => [...prev, { id: item.id, filename: item.filename, status: item.status }]);
      }
    } catch (err: any) {
      setError(String(err?.message || err));
    } finally {
      setUploading(false);
      if (picker.current) picker.current.value = "";
    }
  }

  const waiting = files.some((f) => f.status === "pending");
  const canSend = !!text.trim() && !busy && !running && !waiting && !uploading && !!model;

  async function send() {
    if (!canSend) return;
    const ok = await onSend(
      text.trim(),
      files.filter((f) => f.status === "ready").map((f) => f.id),
      picked ? { mode: "work", skill: picked.skill } : { mode }
    );
    if (ok) {
      setText("");
      setFiles([]);
      setPicked(null); // a skill is for one message; the mode stays
    }
  }

  return (
    <div className="composer-box">
      {files.length ? (
        <div className="attach-row">
          {files.map((f) => (
            <span key={f.id} className={`attach-chip ${f.status}`}>
              <IconFile size={13} /> {f.filename}
              {f.status === "pending" ? " · 扫描中" : f.status === "rejected" ? " · 已拒绝" : ""}
              <button className="link" aria-label={`移除 ${f.filename}`} onClick={() => setFiles(files.filter((x) => x.id !== f.id))}>
                ✕
              </button>
            </span>
          ))}
        </div>
      ) : null}
      {picked ? (
        <div className="skill-tag-row">
          <span className="skill-tag">
            <picked.Icon size={14} /> {picked.label}
            <button className="link" aria-label={`取消 ${picked.label}`} onClick={() => setPicked(null)}>
              ✕
            </button>
          </span>
        </div>
      ) : null}
      <textarea
        ref={area}
        rows={1}
        autoFocus={autoFocus}
        aria-label="消息"
        placeholder={picked?.placeholder || (mode === "chat" ? "直接提问（对话模式：只回答，不做文件）" : placeholder || "输入消息，Enter 发送，Shift + Enter 换行")}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          // isComposing: Enter that confirms a Chinese IME candidate must not send the message
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            send();
          }
        }}
      />
      <div className="composer-tools">
        <button className="icon-btn plus" disabled={uploading} onClick={() => picker.current?.click()} title="添加附件" aria-label="添加附件">
          ＋
        </button>
        <div className="mode-toggle" role="radiogroup" aria-label="模式">
          {(["chat", "work"] as Mode[]).map((m) => (
            <button key={m} role="radio" aria-checked={mode === m} className={mode === m ? "on" : ""} title={MODE_HINT[m]} onClick={() => setMode(m)}>
              {m === "chat" ? "对话" : "工作"}
            </button>
          ))}
        </div>
        {mode === "work"
          ? [
              ...SKILLS.filter((s) => !enabled || enabled.has(s.skill)),
              ...mine.map((m) => ({
                key: `my-${m.name}`,
                skill: m.name,
                label: m.title,
                Icon: IconSparkles,
                placeholder: `说说要做什么，会按你的「${m.title}」技能来做`,
              })),
            ].map((s) => (
              <button
                key={s.key}
                className={`skill-chip ${picked?.key === s.key ? "on" : ""}`}
                onClick={() => pick(s)}
                aria-pressed={picked?.key === s.key}
                aria-label={s.label}
                title={s.label}
              >
                <s.Icon size={14} /> <span className="chip-label">{s.label}</span>
              </button>
            ))
          : null}
        <input ref={picker} type="file" multiple hidden onChange={(e) => e.target.files?.length && upload(e.target.files)} />
        <span className="spacer" />
        <span className="composer-right">
        <select value={model} aria-label="模型" onChange={(e) => onModel(e.target.value)}>
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
              {m.weight !== 1 ? `（消耗 ×${m.weight}）` : ""}
            </option>
          ))}
        </select>
        {error ? <span className="error hint">{error}</span> : null}
        {running ? (
          <button className="secondary" onClick={onStop}>
            ■ 停止
          </button>
        ) : (
          <button onClick={send} disabled={!canSend} aria-label="发送">
            <IconSend size={16} /> <span className="send-label">发送</span>
          </button>
        )}
        </span>
      </div>
    </div>
  );
}
