"use client";

import { DragEvent, useEffect, useRef, useState } from "react";
import { IconDownload, IconFile, IconUpload } from "../../../components/icons";
import { api, API_BASE, downloadUrl, FileItem, Info, redirectIfUnauthorized } from "../../../lib/api";
import { formatBytes, timeAgo } from "../../../lib/format";

function statusView(file: FileItem): { cls: string; text: string } {
  if (file.status === "ready") return { cls: "succeeded", text: "可用" };
  if (file.status === "rejected") return { cls: "failed", text: "已拒绝" };
  return { cls: "queued", text: file.meta?.processing === "error" ? "扫描出错" : "扫描中" };
}

export default function FilesPage() {
  const [files, setFiles] = useState<FileItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [over, setOver] = useState(false);
  const [error, setError] = useState("");
  const [maxUpload, setMaxUpload] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  async function load() {
    try {
      setFiles(await api<FileItem[]>("/v1/files?limit=100"));
      setError("");
    } catch (err: any) {
      setError(String(err?.message || err));
    } finally {
      setLoaded(true);
    }
  }

  useEffect(() => {
    load();
    api<Info>("/v1/info").then((i) => setMaxUpload(i.max_upload_bytes)).catch(() => undefined);
  }, []);

  // Files stay "scanning" until the Worker is done; keep refreshing while any are.
  useEffect(() => {
    if (!files.some((f) => f.status === "pending")) return;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [files]);

  async function remove(file: FileItem) {
    if (!confirm(`删除 ${file.filename}？`)) return;
    try {
      const resp = await fetch(`${API_BASE}/v1/files/${file.id}`, { method: "DELETE" });
      redirectIfUnauthorized(resp);
      if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
      await load();
    } catch (err: any) {
      setError(String(err?.message || err));
    }
  }

  async function upload(list: FileList | File[]) {
    setBusy(true);
    setError("");
    try {
      for (const file of Array.from(list)) {
        const form = new FormData();
        form.append("upload", file);
        const resp = await fetch(`${API_BASE}/v1/files`, { method: "POST", body: form });
        redirectIfUnauthorized(resp);
        if (!resp.ok) throw new Error(`${file.name}: ${resp.status} ${await resp.text()}`);
      }
      await load();
    } catch (err: any) {
      setError(String(err?.message || err));
    } finally {
      if (inputRef.current) inputRef.current.value = "";
      setBusy(false);
    }
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setOver(false);
    if (!busy && event.dataTransfer.files.length) upload(event.dataTransfer.files);
  }

  return (
    <main className="page page-narrow">
      <div className="page-head">
        <h1>资料库</h1>
        <p>文件进对象存储后由 Worker 异步解析、预览、扫描。这里的文件可以在新建任务时作为输入。</p>
      </div>

      <label
        className={`dropzone ${over ? "over" : ""} ${busy ? "disabled" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        <span className="icon">
          <IconUpload />
        </span>
        <strong>{busy ? "上传中…" : "点击选择文件，或拖到这里"}</strong>
        <span className="hint">{maxUpload ? `单个文件最大 ${formatBytes(maxUpload)}；` : ""}上传后会先隔离扫描，通过后才可下载。</span>
        <input ref={inputRef} type="file" multiple disabled={busy} onChange={(event) => event.target.files?.length && upload(event.target.files)} />
      </label>
      {error ? <div className="alert error" style={{ marginTop: 12 }}>{error}</div> : null}

      <div className="section-title" style={{ marginTop: 32 }}>
        文件
        {files.length ? <span className="count">{files.length}</span> : null}
        <span className="spacer" />
        <button className="link" onClick={load}>
          刷新
        </button>
      </div>
      <section className="card">
        {!loaded ? (
          <div className="empty">加载中…</div>
        ) : files.length === 0 ? (
          <div className="empty">
            <strong>还没有文件</strong>上传的文件和任务产出都会出现在这里。
          </div>
        ) : (
          files.map((file) => {
            const st = statusView(file);
            const extract = file.meta?.extract;
            return (
              <div className="file-row" key={file.id}>
                <span className="icon">
                  <IconFile size={18} />
                </span>
                <div className="grow">
                  <div className="name" title={file.filename}>{file.filename}</div>
                  <div className="meta">
                    {formatBytes(file.size)} · v{file.version} · {timeAgo(file.created_at)}
                    {extract?.kind ? ` · ${extract.kind}` : ""}
                    {extract?.pages ? ` · ${extract.pages} 页` : ""}
                    {extract?.slides ? ` · ${extract.slides} 张幻灯片` : ""}
                    {extract?.chars ? ` · ${extract.chars} 字符` : ""}
                  </div>
                  {file.meta?.skill ? (
                    <a className="chip skill-file-chip" href="/skills">
                      个人技能 · {file.meta.skill.title}
                    </a>
                  ) : null}
                </div>
                <span className={`status ${st.cls}`}>{st.text}</span>
                <div className="file-actions">
                  {file.status === "ready" ? (
                    <a className="btn secondary sm" href={downloadUrl(file.download_url)} target="_blank" rel="noreferrer">
                      <IconDownload size={15} /> 下载
                    </a>
                  ) : null}
                  <button className="link danger-text" onClick={() => remove(file)}>
                    删除
                  </button>
                </div>
              </div>
            );
          })
        )}
      </section>
    </main>
  );
}
