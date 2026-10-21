"use client";

import { useEffect, useRef, useState } from "react";
import { api, API_BASE, downloadUrl, FileItem, formatBytes, redirectIfUnauthorized } from "../../lib/api";

export default function FilesPage() {
  const [files, setFiles] = useState<FileItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  async function load() {
    try {
      setFiles(await api<FileItem[]>("/v1/files?limit=100"));
      setError("");
    } catch (err: any) {
      setError(String(err?.message || err));
    }
  }

  useEffect(() => {
    load();
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

  async function upload(file: File) {
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("upload", file);
      const resp = await fetch(`${API_BASE}/v1/files`, { method: "POST", body: form });
      redirectIfUnauthorized(resp);
      if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
      if (inputRef.current) inputRef.current.value = "";
      await load();
    } catch (err: any) {
      setError(String(err?.message || err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="container">
      <h1>资料库</h1>
      <p className="sub">
        文件进对象存储后由 Worker 异步解析、预览、扫描。这里上传的文件可以在新建任务时作为输入。
      </p>

      <section className="panel">
        <h2>上传</h2>
        <div className="row">
          <input
            ref={inputRef}
            type="file"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) upload(file);
            }}
            disabled={busy}
          />
          {busy ? <span className="sub" style={{ margin: 0 }}>上传中…</span> : null}
        </div>
        {error ? <div className="error">{error}</div> : null}
      </section>

      <section className="panel">
        <div className="row" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>文件</h2>
          <div style={{ flex: 1 }} />
          <button className="ghost" onClick={load}>
            刷新
          </button>
        </div>

        {files.length === 0 ? (
          <div className="empty">还没有文件。</div>
        ) : (
          <div className="run-list">
            {files.map((file) => (
              <div className="run-card" key={file.id}>
                <div className="grow">
                  <div className="title">{file.filename}</div>
                  <div className="meta">
                    {formatBytes(file.size)} · v{file.version} · {file.content_type} ·{" "}
                    {new Date(file.created_at).toLocaleString("zh-CN", { hour12: false })}
                  </div>
                  {file.meta?.extract?.kind ? (
                    <div className="meta" style={{ marginTop: 4 }}>
                      {file.meta.extract.kind}
                      {file.meta.extract.pages ? ` · ${file.meta.extract.pages} 页` : ""}
                      {file.meta.extract.slides ? ` · ${file.meta.extract.slides} 张幻灯片` : ""}
                      {file.meta.extract.chars ? ` · ${file.meta.extract.chars} 字符` : ""}
                    </div>
                  ) : null}
                </div>
                <span className={`status ${file.status === "ready" ? "succeeded" : file.status === "rejected" ? "failed" : "queued"}`}>
                  {file.status === "pending" ? (file.meta?.processing === "error" ? "扫描出错" : "扫描中") : file.status === "rejected" ? "已拒绝" : file.status}
                </span>
                {file.status === "ready" ? (
                  <a href={downloadUrl(file.download_url)} target="_blank" rel="noreferrer">
                    下载
                  </a>
                ) : null}
                <a href="#" onClick={(event) => { event.preventDefault(); remove(file); }}>
                  删除
                </a>
              </div>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}
