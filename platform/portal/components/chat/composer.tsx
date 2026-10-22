"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE, api, FileItem, ModelInfo, redirectIfUnauthorized } from "../../lib/api";
import { IconFile, IconSend } from "../icons";

type Attachment = { id: string; filename: string; status: string };

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
  onSend: (prompt: string, fileIds: string[]) => Promise<boolean>;
  onStop: () => void;
  autoFocus?: boolean;
  placeholder?: string;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const [error, setError] = useState("");
  const area = useRef<HTMLTextAreaElement | null>(null);
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
    const timer = setInterval(async () => {
      const fresh = await Promise.all(
        files.map((f) => (f.status === "pending" ? api<FileItem>(`/v1/files/${f.id}`).then((x) => ({ ...f, status: x.status })).catch(() => f) : f))
      );
      setFiles(fresh);
    }, 1500);
    return () => clearInterval(timer);
  }, [files]);

  async function upload(list: FileList) {
    setError("");
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
    if (picker.current) picker.current.value = "";
  }

  const waiting = files.some((f) => f.status === "pending");
  const canSend = !!text.trim() && !busy && !running && !waiting && !!model;

  async function send() {
    if (!canSend) return;
    const ok = await onSend(
      text.trim(),
      files.filter((f) => f.status === "ready").map((f) => f.id)
    );
    if (ok) {
      setText("");
      setFiles([]);
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
      <textarea
        ref={area}
        rows={1}
        autoFocus={autoFocus}
        aria-label="消息"
        placeholder={placeholder || "输入消息，Enter 发送，Shift + Enter 换行"}
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
        <button className="link" onClick={() => picker.current?.click()} title="添加附件" aria-label="添加附件">
          📎 附件
        </button>
        <input ref={picker} type="file" multiple hidden onChange={(e) => e.target.files?.length && upload(e.target.files)} />
        <select value={model} aria-label="模型" onChange={(e) => onModel(e.target.value)}>
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
              {m.weight !== 1 ? `（消耗 ×${m.weight}）` : ""}
            </option>
          ))}
        </select>
        <span className="spacer" />
        {error ? <span className="error hint">{error}</span> : null}
        {running ? (
          <button className="secondary" onClick={onStop}>
            ■ 停止
          </button>
        ) : (
          <button onClick={send} disabled={!canSend} aria-label="发送">
            <IconSend size={16} /> 发送
          </button>
        )}
      </div>
    </div>
  );
}
