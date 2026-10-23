"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { api, Conversation, Me, Usage } from "../lib/api";
import { formatTokens, timeAgo } from "../lib/format";
import { useProjects } from "../lib/projects";
import { IconFolder, IconLibrary, IconLogOut, IconNewChat, IconPanel, IconPlus, IconSettings, IconSparkles } from "./icons";
import ConversationMenu from "./conversation-menu";
import { NewProjectInput } from "./library/projects";

/** Pages tell the sidebar that the conversation list changed (a new one, a title, a finished answer). */
export const CONVERSATIONS_CHANGED = "ailab:conversations";
export function conversationsChanged(): void {
  window.dispatchEvent(new Event(CONVERSATIONS_CHANGED));
}

function UserMenu({ me }: { me: Me | null }) {
  const [open, setOpen] = useState(false);
  const [usage, setUsage] = useState<Usage | null>(null);
  const box = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    api<Usage>("/v1/usage").then(setUsage).catch(() => setUsage(null));
    const close = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.href = "/login";
  }

  const name = me?.display_name || me?.username || "";
  const pct = usage ? Math.min(100, Math.round((usage.daily_tokens.used / Math.max(1, usage.daily_tokens.limit)) * 100)) : 0;
  return (
    <div className="user-menu" ref={box}>
      {open ? (
        <div className="user-pop" role="menu">
          <div className="user-pop-head">
            <div className="user-pop-name">{name}</div>
            <div className="hint">{me?.username}</div>
          </div>
          {usage ? (
            <div className="user-pop-usage" title="Token 按模型倍率折算">
              <div className="meter-head">
                <span>今日额度已用 {pct}%</span>
                <span>
                  {formatTokens(usage.daily_tokens.used)} / {formatTokens(usage.daily_tokens.limit)}
                </span>
              </div>
              <div className="meter-bar">
                <div className={`meter-fill ${pct >= 100 ? "full" : pct >= 80 ? "warn" : ""}`} style={{ width: `${pct}%` }} />
              </div>
            </div>
          ) : null}
          <a className="menu-item" href="/account" role="menuitem">
            <IconSettings size={16} /> 账号设置
          </a>
          <button className="menu-item" onClick={logout} role="menuitem">
            <IconLogOut size={16} /> 退出登录
          </button>
        </div>
      ) : null}
      {me ? (
        <button className="user-chip" onClick={() => setOpen(!open)} aria-expanded={open} title="账号">
          <span className="avatar">{(name[0] || "?").toUpperCase()}</span>
          <span className="user-name">{name}</span>
        </button>
      ) : null}
    </div>
  );
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "/";
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [addingProject, setAddingProject] = useState(false);
  const projects = useProjects();
  const current = pathname.startsWith("/c/") ? pathname.slice(3) : null;
  const currentProject = pathname.startsWith("/p/") ? pathname.slice(3) : null;

  const load = useCallback(() => api<Conversation[]>("/v1/conversations").then(setConversations).catch(() => undefined), []);

  useEffect(() => {
    api<Me>("/v1/auth/me").then(setMe).catch(() => setMe(null));
    // remembered on wide screens; narrow screens start with the sidebar closed (it covers the page there)
    setCollapsed(window.innerWidth < 760 || localStorage.getItem("ailab.sidebar") === "closed");
    load();
    window.addEventListener(CONVERSATIONS_CHANGED, load);
    return () => window.removeEventListener(CONVERSATIONS_CHANGED, load);
  }, [load]);

  // a conversation that is still answering: keep its "回答中…" fresh
  useEffect(() => {
    if (!conversations?.some((c) => c.last_status === "running" || c.last_status === "queued")) return;
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [conversations, load]);

  useEffect(() => {
    if (window.innerWidth < 760) setCollapsed(true); // on a phone, navigating closes the drawer
  }, [pathname]);

  function toggle() {
    const next = !collapsed;
    setCollapsed(next);
    if (window.innerWidth >= 760) localStorage.setItem("ailab.sidebar", next ? "closed" : "open");
  }

  return (
    <div className={`shell ${collapsed ? "collapsed" : ""}`}>
      <aside className="sidebar" aria-label="侧栏">
        <div className="sidebar-head">
          <a className="brand" href="/">
            <span className="brand-mark">A</span>
            AILab
          </a>
          <button className="icon-btn" onClick={toggle} aria-label="收起侧栏" title="收起侧栏">
            <IconPanel size={18} />
          </button>
        </div>
        <nav className="side-nav" aria-label="主导航">
          <a href="/" className={`side-link ${pathname === "/" ? "on" : ""}`}>
            <IconNewChat size={17} /> 新对话
          </a>
          <a href="/files" className={`side-link ${pathname.startsWith("/files") ? "on" : ""}`}>
            <IconLibrary size={17} /> 资料库
          </a>
          <a href="/skills" className={`side-link ${pathname.startsWith("/skills") ? "on" : ""}`}>
            <IconSparkles size={17} /> 技能
          </a>
        </nav>
        <div className="side-scroll">
        <div className="side-section">
          项目
          <button className="icon-btn side-add" onClick={() => setAddingProject(true)} aria-label="新建项目" title="新建项目">
            <IconPlus size={14} />
          </button>
        </div>
        <nav className="side-projects" aria-label="项目">
          {addingProject ? (
            <NewProjectInput
              onCreated={(p) => {
                setAddingProject(false);
                router.push(`/p/${p.id}`);
              }}
              onCancel={() => setAddingProject(false)}
            />
          ) : null}
          {(projects || []).map((p) => (
            <a key={p.id} href={`/p/${p.id}`} className={`side-link project ${p.id === currentProject ? "on" : ""}`} title={p.name}>
              <IconFolder size={16} /> <span className="conv-title">{p.name}</span>
            </a>
          ))}
        </nav>
        <div className="side-section">最近</div>
        <nav className="conv-list" aria-label="对话列表">
          {conversations && conversations.length === 0 ? <div className="hint conv-empty">还没有对话</div> : null}
          {(conversations || []).map((c) => (
            <div key={c.id} className={`conv-item ${c.id === current ? "on" : ""}`}>
              <a href={`/c/${c.id}`} title={c.title}>
                <span className="conv-title">{c.title || "新对话"}</span>
                <span className="conv-time">{c.last_status === "running" || c.last_status === "queued" ? "回答中…" : timeAgo(c.updated_at)}</span>
              </a>
              <ConversationMenu conv={c} onDeleted={() => c.id === current && router.push("/")} />
            </div>
          ))}
        </nav>
        </div>
        <UserMenu me={me} />
      </aside>
      <div className="sidebar-scrim" onClick={toggle} aria-hidden="true" />

      <div className="main">
        {collapsed ? (
          <div className="main-top">
            <button className="icon-btn" onClick={toggle} aria-label="展开侧栏" title="展开侧栏">
              <IconPanel size={18} />
            </button>
            <a className="icon-btn" href="/" aria-label="新对话" title="新对话">
              <IconNewChat size={18} />
            </a>
          </div>
        ) : null}
        {children}
      </div>
    </div>
  );
}
