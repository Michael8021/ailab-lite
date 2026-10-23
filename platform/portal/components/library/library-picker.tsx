"use client";

import { useEffect, useState } from "react";
import { FileItem } from "../../lib/api";
import { formatBytes, timeAgo } from "../../lib/format";
import { IconCheck, IconSearch } from "../icons";
import FileTile from "./file-tile";
import Modal from "./modal";
import { Category, useLibrary } from "./use-library";

type Tab = Category | "project";

/** Pick ready files from the library to attach to a message. */
export default function LibraryPicker({
  projectId,
  attached,
  onPick,
  onClose,
}: {
  projectId?: string | null;
  attached: string[]; // already on the message
  onPick: (files: FileItem[]) => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>(projectId ? "project" : "all");
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const [chosen, setChosen] = useState<Map<string, FileItem>>(new Map());

  useEffect(() => {
    const timer = setTimeout(() => setQ(search), 250);
    return () => clearTimeout(timer);
  }, [search]);

  const { files, loaded, more, loadMore } = useLibrary({
    category: tab === "project" ? "all" : tab,
    projectId: tab === "project" ? projectId : null,
    q,
  });

  function toggle(f: FileItem) {
    const next = new Map(chosen);
    if (next.has(f.id)) next.delete(f.id);
    else next.set(f.id, f);
    setChosen(next);
  }

  const tabs: [Tab, string][] = [
    ...(projectId ? ([["project", "本项目"]] as [Tab, string][]) : []),
    ["all", "全部"],
    ["starred", "收藏"],
    ["images", "图像"],
  ];

  return (
    <Modal
      title="从资料库选择"
      onClose={onClose}
      className="modal-picker"
      footer={
        <>
          <span className="hint">{chosen.size ? `已选 ${chosen.size} 个` : ""}</span>
          <span className="spacer" />
          <button className="secondary" onClick={onClose}>
            取消
          </button>
          <button disabled={!chosen.size} onClick={() => onPick([...chosen.values()])}>
            添加
          </button>
        </>
      }
    >
      <div className="picker-bar">
        <div className="seg" role="tablist">
          {tabs.map(([key, label]) => (
            <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? "on" : ""} onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
        </div>
        <label className="search">
          <IconSearch size={15} />
          <input type="text" placeholder="搜索" aria-label="搜索文件" value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
      </div>
      <div className="picker-list">
        {!loaded ? (
          <div className="empty">加载中…</div>
        ) : files.length === 0 ? (
          <div className="empty">{q ? "没有匹配的文件" : "这里还没有文件"}</div>
        ) : (
          files.map((f) => {
            const already = attached.includes(f.id);
            const usable = f.status === "ready" && !already;
            const on = already || chosen.has(f.id);
            return (
              <button key={f.id} className={`picker-row ${on ? "on" : ""}`} disabled={!usable} onClick={() => toggle(f)}>
                <span className={`check ${on ? "on" : ""}`}>{on ? <IconCheck size={12} /> : null}</span>
                <FileTile file={f} size={32} />
                <span className="grow">
                  <span className="name">{f.filename}</span>
                  <span className="meta">
                    {f.status === "pending" ? "扫描中" : f.status === "rejected" ? "已拒绝" : `${formatBytes(f.size)} · ${timeAgo(f.created_at)}`}
                  </span>
                </span>
              </button>
            );
          })
        )}
        {more ? (
          <button className="link more" onClick={loadMore}>
            加载更多
          </button>
        ) : null}
      </div>
    </Modal>
  );
}
