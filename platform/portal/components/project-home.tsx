"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, Conversation, downloadUrl, Project } from "../lib/api";
import { formatBytes, timeAgo } from "../lib/format";
import { projectsChanged } from "../lib/projects";
import { conversationsChanged, CONVERSATIONS_CHANGED } from "./app-shell";
import { IconDownload, IconFolder, IconMore, IconPencil, IconTrash } from "./icons";
import ConversationMenu from "./conversation-menu";
import FileTile from "./library/file-tile";
import { useLibrary } from "./library/use-library";

/** A project's front page: start a chat in it, and see what is already in it. */
export default function ProjectHome({ project, composer, error }: { project: Project; composer: React.ReactNode; error: string }) {
  const router = useRouter();
  const [tab, setTab] = useState<"chats" | "files">("chats");
  const [chats, setChats] = useState<Conversation[] | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(project.name);
  const [menu, setMenu] = useState(false);
  const menuBox = useRef<HTMLDivElement | null>(null);
  const { files, loaded } = useLibrary({ category: "all", projectId: project.id }, tab === "files"); // only once opened

  useEffect(() => setName(project.name), [project.name]);

  useEffect(() => {
    const load = () =>
      api<Conversation[]>(`/v1/conversations?project_id=${encodeURIComponent(project.id)}`)
        .then(setChats)
        .catch(() => setChats([]));
    load();
    window.addEventListener(CONVERSATIONS_CHANGED, load);
    return () => window.removeEventListener(CONVERSATIONS_CHANGED, load);
  }, [project.id]);

  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent) => menuBox.current && !menuBox.current.contains(e.target as Node) && setMenu(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [menu]);

  async function rename() {
    setRenaming(false);
    const value = name.trim();
    if (!value || value === project.name) return setName(project.name);
    await api(`/v1/projects/${project.id}`, { method: "PATCH", body: JSON.stringify({ name: value }) }).catch(() => setName(project.name));
    projectsChanged();
  }

  async function remove() {
    setMenu(false);
    const what = [project.conversations ? `${project.conversations} 个对话` : "", project.files ? `${project.files} 个文件` : ""].filter(Boolean).join("和");
    if (!confirm(`删除项目「${project.name}」？${what ? `其中的 ${what}会一起删除，` : ""}无法恢复。`)) return;
    await api(`/v1/projects/${project.id}`, { method: "DELETE" });
    projectsChanged();
    conversationsChanged();
    router.push("/");
  }

  return (
    <div className="project-home">
      <div className="project-head">
        <span className="project-mark">
          <IconFolder size={20} />
        </span>
        {renaming ? (
          <input
            type="text"
            className="project-name-input"
            autoFocus
            maxLength={120}
            aria-label="项目名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={rename}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) e.currentTarget.blur();
              if (e.key === "Escape") {
                setName(project.name);
                setRenaming(false);
              }
            }}
          />
        ) : (
          <h1 onDoubleClick={() => setRenaming(true)}>{name}</h1>
        )}
        <div className="head-menu" ref={menuBox}>
          <button className="icon-btn" onClick={() => setMenu(!menu)} aria-label="项目操作" aria-expanded={menu}>
            <IconMore size={18} />
          </button>
          {menu ? (
            <div className="pop-menu right" role="menu">
              <button
                className="menu-item"
                role="menuitem"
                onClick={() => {
                  setMenu(false);
                  setRenaming(true);
                }}
              >
                <IconPencil size={15} /> 重命名
              </button>
              <button className="menu-item danger" role="menuitem" onClick={remove}>
                <IconTrash size={15} /> 删除项目
              </button>
            </div>
          ) : null}
        </div>
      </div>

      {error ? <div className="alert error">{error}</div> : null}
      {composer}

      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "chats"} className={tab === "chats" ? "on" : ""} onClick={() => setTab("chats")}>
          对话{(chats?.length ?? project.conversations) ? <span className="tab-count">{chats?.length ?? project.conversations}</span> : null}
        </button>
        <button role="tab" aria-selected={tab === "files"} className={tab === "files" ? "on" : ""} onClick={() => setTab("files")}>
          文件{project.files ? <span className="tab-count">{project.files}</span> : null}
        </button>
        <span className="spacer" />
        {tab === "files" && project.files ? (
          <a className="tab-link" href={`/files?project=${project.id}`}>
            在资料库中管理
          </a>
        ) : null}
      </div>

      {tab === "chats" ? (
        chats === null ? null : chats.length === 0 ? (
          <div className="project-empty">在上面开始这个项目的第一个对话</div>
        ) : (
          <div className="project-list">
            {chats.map((c) => (
              <div key={c.id} className="project-row conv-row">
                <a className="grow name" href={`/c/${c.id}`}>
                  {c.title || "新对话"}
                </a>
                <span className="meta">{c.last_status === "running" || c.last_status === "queued" ? "回答中…" : timeAgo(c.updated_at)}</span>
                <ConversationMenu conv={c} />
              </div>
            ))}
          </div>
        )
      ) : !loaded ? null : files.length === 0 ? (
        <div className="project-empty">在这个项目的对话里上传或生成的文件会放在这里</div>
      ) : (
        <div className="project-list">
          {files.map((f) => (
            <div key={f.id} className="project-row">
              <FileTile file={f} size={30} />
              <span className="grow name">{f.filename}</span>
              <span className="meta">
                {f.status === "pending" ? "扫描中" : f.status === "rejected" ? "已拒绝" : `${formatBytes(f.size)} · ${timeAgo(f.created_at)}`}
              </span>
              {f.status === "ready" ? (
                <a className="icon-btn" href={downloadUrl(f.download_url)} aria-label={`下载 ${f.filename}`} title="下载">
                  <IconDownload size={16} />
                </a>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
