"use client";

import { useEffect, useMemo, useState } from "react";
import { Preview, previewUrl } from "../../lib/api";
import { IconDownload } from "../icons";

type Cell = { v: string; b?: 1; bg?: string; fg?: string; n?: 1; f?: string };
type Sheet = { name: string; rows: Cell[][]; widths: (number | null)[]; truncated: boolean; size: [number, number]; charts: number };

function columnName(i: number): string {
  let s = "";
  for (let n = i + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
}

function Pages({ preview }: { preview: Preview }) {
  const pages = preview.pages || [];
  const [index, setIndex] = useState(0);
  useEffect(() => setIndex((i) => Math.min(i, Math.max(0, pages.length - 1))), [pages.length]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.closest("textarea, input")) return;
      if (e.key === "ArrowRight") setIndex((i) => Math.min(pages.length - 1, i + 1));
      if (e.key === "ArrowLeft") setIndex((i) => Math.max(0, i - 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pages.length]);
  if (!pages.length || !preview.run_id) return <div className="hint">没有可显示的页面</div>;
  const runId = preview.run_id;
  return (
    <div className="pages">
      <div className="page-stage">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={previewUrl(runId, pages[index])} alt={`第 ${index + 1} 页`} />
        <div className="page-nav">
          <button className="secondary sm" disabled={index === 0} onClick={() => setIndex(index - 1)} aria-label="上一页">
            ‹
          </button>
          <span className="hint">
            {index + 1} / {pages.length}
          </span>
          <button className="secondary sm" disabled={index >= pages.length - 1} onClick={() => setIndex(index + 1)} aria-label="下一页">
            ›
          </button>
        </div>
      </div>
      <div className="thumbs">
        {pages.map((p, i) => (
          <button key={p} className={`thumb ${i === index ? "on" : ""}`} onClick={() => setIndex(i)} aria-label={`第 ${i + 1} 页`}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={previewUrl(runId, p)} alt="" loading="lazy" />
            <span>{i + 1}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function Sheets({ preview }: { preview: Preview }) {
  const [data, setData] = useState<{ sheets: Sheet[]; errors: string[] } | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState(0);
  useEffect(() => {
    if (!preview.run_id || !preview.data) return;
    let stale = false;
    fetch(previewUrl(preview.run_id, preview.data))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((d) => !stale && (setData(d), setError("")))
      .catch((e) => !stale && setError(String(e?.message || e)));
    return () => {
      stale = true;
    };
  }, [preview.run_id, preview.data]);
  if (error) return <div className="alert error">预览加载失败：{error}</div>;
  if (!data) return <div className="hint">加载中…</div>;
  const sheet = data.sheets[Math.min(tab, data.sheets.length - 1)];
  if (!sheet) return <div className="hint">空工作簿</div>;
  const cols = Math.max(0, ...sheet.rows.map((r) => r.length));
  return (
    <div className="sheets">
      {data.errors?.length ? <div className="alert error">公式错误：{data.errors.slice(0, 5).join("，")}</div> : null}
      <div className="sheet-grid">
        <table>
          <thead>
            <tr>
              <th className="corner" />
              {Array.from({ length: cols }, (_, j) => (
                <th key={j} style={{ minWidth: Math.max(48, Math.min(320, (sheet.widths[j] || 9) * 7.5)) }}>
                  {columnName(j)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sheet.rows.map((row, i) => (
              <tr key={i}>
                <th>{i + 1}</th>
                {Array.from({ length: cols }, (_, j) => {
                  const c = row[j] || { v: "" };
                  return (
                    <td
                      key={j}
                      title={c.f || undefined}
                      className={`${c.n ? "num" : ""} ${c.f ? "formula" : ""}`}
                      style={{ background: c.bg ? `#${c.bg}` : undefined, color: c.fg ? `#${c.fg}` : undefined, fontWeight: c.b ? 650 : undefined }}
                    >
                      {c.v}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="sheet-tabs">
        {data.sheets.map((s, i) => (
          <button key={s.name} className={`sheet-tab ${i === tab ? "on" : ""}`} onClick={() => setTab(i)}>
            {s.name}
          </button>
        ))}
        <span className="hint">
          {sheet.truncated ? `只显示前 ${sheet.rows.length} 行；共 ${sheet.size[0]} 行 × ${sheet.size[1]} 列` : ""}
          {sheet.charts ? ` · 含 ${sheet.charts} 个图表（下载后查看）` : ""}
        </span>
      </div>
    </div>
  );
}

export default function PreviewPane({
  previews,
  selected,
  onSelect,
  onClose,
  live,
  downloads,
}: {
  previews: Preview[];
  selected: string | null;
  onSelect: (filename: string) => void;
  onClose: () => void;
  live: boolean;
  downloads: Record<string, string>;
}) {
  const current = useMemo(() => previews.find((p) => p.filename === selected) || previews[previews.length - 1], [previews, selected]);
  if (!current) return null;
  return (
    <aside className="preview-pane" aria-label="文件预览">
      <div className="preview-head">
        <div className="preview-tabs">
          {previews.map((p) => (
            <button key={p.filename} className={`preview-tab ${p.filename === current.filename ? "on" : ""}`} onClick={() => onSelect(p.filename)}>
              {p.filename}
            </button>
          ))}
        </div>
        <span className="spacer" />
        {live ? <span className="live on">实时</span> : null}
        {downloads[current.filename] ? (
          <a className="btn secondary sm" href={downloads[current.filename]} target="_blank" rel="noreferrer" aria-label={`下载 ${current.filename}`}>
            <IconDownload size={15} />
          </a>
        ) : null}
        <button className="link" onClick={onClose} aria-label="关闭预览">
          ✕
        </button>
      </div>
      <div className="preview-body" key={current.filename}>
        {current.kind === "pages" ? <Pages preview={current} /> : current.kind === "sheets" ? <Sheets preview={current} /> : null}
      </div>
    </aside>
  );
}
