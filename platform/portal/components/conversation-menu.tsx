"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, Conversation } from "../lib/api";
import { projectsChanged } from "../lib/projects";
import { conversationsChanged } from "./app-shell";
import { IconFolderInput, IconMore, IconTrash } from "./icons";
import { MoveDialog } from "./library/projects";

/** The open conversation page follows a move made from the sidebar (its breadcrumb, where uploads go). */
export const CONVERSATION_MOVED = "ailab:conversation-moved";

const MENU_W = 176;
const MENU_H = 92;

/** "⋯" on a conversation: move it into / out of a project, or delete it. */
export default function ConversationMenu({ conv, onDeleted, className = "" }: { conv: Conversation; onDeleted?: () => void; className?: string }) {
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);
  const [moving, setMoving] = useState(false);
  const button = useRef<HTMLButtonElement | null>(null);
  const menu = useRef<HTMLDivElement | null>(null);
  const title = conv.title || "新对话";

  useEffect(() => {
    if (!at) return;
    const close = (e: Event) => {
      if (e.type === "mousedown" && (menu.current?.contains(e.target as Node) || button.current?.contains(e.target as Node))) return;
      setAt(null);
    };
    document.addEventListener("mousedown", close);
    window.addEventListener("scroll", close, true); // a fixed menu would drift away from its row
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", close);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [at]);

  // Fixed, not absolute: the sidebar list scrolls and would clip a menu that hangs out of it.
  function open() {
    if (at) return setAt(null);
    const r = button.current!.getBoundingClientRect();
    const below = r.bottom + 4 + MENU_H < window.innerHeight;
    setAt({ top: below ? r.bottom + 4 : r.top - 4 - MENU_H, left: Math.max(8, r.right - MENU_W) });
  }

  async function move(projectId: string | null) {
    setMoving(false);
    if (projectId === (conv.project_id || null)) return;
    const ok = await api(`/v1/conversations/${conv.id}`, { method: "PATCH", body: JSON.stringify({ project_id: projectId }) }).then(
      () => true,
      () => false
    );
    if (ok) window.dispatchEvent(new CustomEvent(CONVERSATION_MOVED, { detail: { id: conv.id, project_id: projectId } }));
    conversationsChanged();
    projectsChanged();
  }

  async function remove() {
    setAt(null);
    if (!confirm(`删除对话「${title}」？（产出的文件仍保留在资料库）`)) return;
    await api(`/v1/conversations/${conv.id}`, { method: "DELETE" }).catch(() => undefined);
    conversationsChanged();
    if (conv.project_id) projectsChanged();
    onDeleted?.();
  }

  return (
    <>
      <button
        ref={button}
        className={`link conv-more ${at ? "open" : ""} ${className}`}
        aria-label={`更多操作 ${title}`}
        aria-expanded={!!at}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          open();
        }}
      >
        <IconMore size={16} />
      </button>
      {at
        ? createPortal(
        <div ref={menu} className="pop-menu fixed" role="menu" style={{ top: at.top, left: at.left, width: MENU_W }}>
          <button
            className="menu-item"
            role="menuitem"
            onClick={() => {
              setAt(null);
              setMoving(true);
            }}
          >
            <IconFolderInput size={15} /> {conv.project_id ? "移动到…" : "移到项目"}
          </button>
          <button className="menu-item danger" role="menuitem" onClick={remove}>
            <IconTrash size={15} /> 删除
          </button>
        </div>,
            document.body
          )
        : null}
      {moving ? <MoveDialog title={`移动「${title}」`} current={conv.project_id || null} onPick={move} onClose={() => setMoving(false)} /> : null}
    </>
  );
}
