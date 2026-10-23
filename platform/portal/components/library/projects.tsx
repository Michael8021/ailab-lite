"use client";

import { useState } from "react";
import { Project } from "../../lib/api";
import { createProject, useProjects } from "../../lib/projects";
import { IconCheck, IconFolder, IconFolderPlus, IconX } from "../icons";
import Modal from "./modal";

/** A one-line name field that creates a project on Enter. */
export function NewProjectInput({ onCreated, onCancel }: { onCreated: (p: Project) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit() {
    const value = name.trim();
    if (!value || busy) return;
    setBusy(true);
    try {
      onCreated(await createProject(value));
    } catch (err: any) {
      setError(String(err?.message || err));
      setBusy(false);
    }
  }

  return (
    <div className="new-project">
      <span className="new-project-icon">
        <IconFolder size={16} />
      </span>
      <input
        type="text"
        autoFocus
        maxLength={120}
        placeholder="项目名称"
        aria-label="项目名称"
        value={name}
        disabled={busy}
        title={error || undefined}
        className={error ? "invalid" : ""}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.nativeEvent.isComposing) submit();
          if (e.key === "Escape") onCancel();
        }}
        onBlur={() => !name.trim() && onCancel()}
      />
      <button className="icon-btn" onMouseDown={(e) => e.preventDefault()} onClick={onCancel} aria-label="取消">
        <IconX size={14} />
      </button>
    </div>
  );
}

/** Choose where files go: a project, or no project at all. */
export function MoveDialog({
  title,
  current,
  onPick,
  onClose,
}: {
  title: string;
  current?: string | null; // the project they are all in now, if they share one
  onPick: (projectId: string | null) => void;
  onClose: () => void;
}) {
  const projects = useProjects();
  const [adding, setAdding] = useState(false);
  const row = (id: string | null, label: string, icon: React.ReactNode) => (
    <button key={id || "none"} className={`pick-row ${current === id ? "on" : ""}`} onClick={() => onPick(id)}>
      {icon}
      <span className="pick-name">{label}</span>
      {current === id ? <IconCheck size={16} /> : null}
    </button>
  );
  return (
    <Modal title={title} onClose={onClose} className="modal-sm">
      <div className="pick-list">
        {row(null, "不属于任何项目", <span className="pick-none" />)}
        {(projects || []).map((p) => row(p.id, p.name, <IconFolder size={17} />))}
        {adding ? (
          <NewProjectInput onCreated={(p) => onPick(p.id)} onCancel={() => setAdding(false)} />
        ) : (
          <button className="pick-row add" onClick={() => setAdding(true)}>
            <IconFolderPlus size={17} />
            <span className="pick-name">新建项目</span>
          </button>
        )}
      </div>
    </Modal>
  );
}
