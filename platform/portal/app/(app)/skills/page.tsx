"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { IconChart, IconDownload, IconSheet, IconSlides, IconSparkles, IconUpload } from "../../../components/icons";
import { API_BASE, api, FileItem, redirectIfUnauthorized, Skill } from "../../../lib/api";

const ICONS: Record<string, (p: { size?: number }) => React.ReactElement> = { pptx: IconSlides, xlsx: IconSheet, data: IconChart };

const TEMPLATE = `---
name: weekly-report
title: 周报助手
description: 按团队模板写周报：本周完成、问题与风险、下周计划
---

# 什么时候用
用户要写周报、整理一周的工作时。

# 怎么做
1. 先弄清楚本周做了什么：用户给的文字、上传的材料，不清楚就问。
2. 按下面的结构写，每部分 3～5 条，每条一句话，先写结果再写过程：
   - 本周完成
   - 问题与风险（写清楚需要谁帮忙）
   - 下周计划（带时间点）
3. 需要交付文件时，生成 Word 或 Markdown 放到 /workspace/outputs/。

# 可以附带脚本
把这个 SKILL.md 和 scripts/、assets/ 等文件夹一起打成 .zip 上传，
说明里用相对路径引用，比如：python scripts/fill.py
`;

function Switch({ on, label, onChange, disabled }: { on: boolean; label: string; onChange: (on: boolean) => void; disabled?: boolean }) {
  return (
    <button role="switch" aria-checked={on} aria-label={label} className={`switch ${on ? "on" : ""}`} disabled={disabled} onClick={() => onChange(!on)}>
      <span className="knob" />
    </button>
  );
}

function SkillRow({ s, busy, onToggle, onDelete }: { s: Skill; busy: boolean; onToggle: (on: boolean) => void; onDelete?: () => void }) {
  const Icon = ICONS[s.name] || IconSparkles;
  return (
    <div className={`skill-row ${s.enabled ? "" : "off"} ${s.error ? "broken" : ""}`}>
      <span className="skill-icon">
        <Icon size={20} />
      </span>
      <div className="grow">
        <div className="skill-title">
          {s.title}
          {s.name ? <span className="chip mono">{s.name}</span> : null}
          {s.scope === "personal" ? <span className="chip">{s.kind === "zip" ? `压缩包 · ${s.files} 个文件` : "SKILL.md"}</span> : <span className="chip">内置</span>}
        </div>
        {s.description ? <div className="skill-desc">{s.description}</div> : null}
        {s.scope === "personal" ? (
          <div className="skill-file hint">
            资料库文件：{s.filename}
            {s.error ? <span className="error">　无法使用：{s.error}</span> : null}
          </div>
        ) : null}
      </div>
      {onDelete ? (
        <button className="link danger-text" onClick={onDelete} title="删除这个技能（同时从资料库删除文件）">
          删除
        </button>
      ) : null}
      <Switch on={s.enabled} label={`${s.enabled ? "关闭" : "开启"} ${s.title}`} disabled={busy || !!s.error} onChange={onToggle} />
    </div>
  );
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [uploading, setUploading] = useState<string[]>([]); // file names still being scanned
  const [error, setError] = useState("");
  const picker = useRef<HTMLInputElement | null>(null);

  const load = useCallback(
    () =>
      api<Skill[]>("/v1/skills")
        .then(setSkills)
        .catch((e) => setError(String(e?.message || e))),
    []
  );
  useEffect(() => {
    load();
  }, [load]);

  async function toggle(s: Skill, enabled: boolean) {
    setBusy(s.name);
    setError("");
    setSkills((list) => list?.map((x) => (x.name === s.name && x.scope === s.scope ? { ...x, enabled } : x)) || list); // optimistic
    try {
      await api(`/v1/skills/${s.name}`, { method: "PUT", body: JSON.stringify({ enabled }) });
    } catch (e: any) {
      setError(`没能保存：${e?.message || e}`);
      load();
    } finally {
      setBusy(null);
    }
  }

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
      setUploading((u) => [...u, file.name]);
      // the Worker scans the file and recognises the skill; wait for it
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const f = await api<FileItem>(`/v1/files/${item.id}`).catch(() => null);
        if (f && f.status !== "pending") {
          if (f.status === "rejected") setError(`${file.name}：没有通过安全扫描`);
          else if (!f.meta?.skill && !f.meta?.skill_error) setError(`${file.name}：不是技能文件（需要带 name、description 的 SKILL.md，或包含它的 .zip）`);
          break;
        }
      }
      setUploading((u) => u.filter((n) => n !== file.name));
      await load();
    }
    if (picker.current) picker.current.value = "";
  }

  async function remove(s: Skill) {
    if (!s.file_id || !confirm(`删除个人技能「${s.title}」？资料库里的文件 ${s.filename} 也会删除。`)) return;
    const resp = await fetch(`${API_BASE}/v1/files/${s.file_id}`, { method: "DELETE" });
    redirectIfUnauthorized(resp);
    if (!resp.ok) setError(`删除失败（${resp.status}）`);
    load();
  }

  function downloadTemplate() {
    const url = URL.createObjectURL(new Blob([TEMPLATE], { type: "text/markdown;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "SKILL.md" });
    a.click();
    URL.revokeObjectURL(url);
  }

  const builtin = (skills || []).filter((s) => s.scope === "builtin");
  const personal = (skills || []).filter((s) => s.scope === "personal");

  return (
    <main className="page page-narrow">
      <div className="page-head">
        <h1>技能</h1>
        <p>技能是 AI 做特定工作时照着做的说明和工具。开启的技能会在需要时自动使用，也可以在输入框里直接点选；关闭后 AI 不会再用它。</p>
      </div>
      {error ? <div className="alert error" style={{ marginBottom: 12 }}>{error}</div> : null}

      <h2 className="section-title">通用技能</h2>
      <p className="hint section-hint">平台提供，所有人都可以用。</p>
      <section className="card skill-list">
        {skills === null ? <div className="empty">加载中…</div> : builtin.map((s) => <SkillRow key={s.name} s={s} busy={busy === s.name} onToggle={(on) => toggle(s, on)} />)}
      </section>

      <div className="section-title" style={{ marginTop: 32 }}>
        个人技能
        <span className="spacer" />
        <button className="link" onClick={downloadTemplate}>
          <IconDownload size={15} /> 下载模板
        </button>
        <button className="secondary sm" onClick={() => picker.current?.click()}>
          <IconUpload size={15} /> 上传技能
        </button>
        <input ref={picker} type="file" accept=".md,.zip" multiple hidden onChange={(e) => e.target.files?.length && upload(e.target.files)} />
      </div>
      <p className="hint section-hint">保存在你自己的资料库里，只有你能看到和使用。一个 SKILL.md，或者 SKILL.md 连同脚本、素材打成的 .zip。</p>
      <section className="card skill-list">
        {uploading.map((n) => (
          <div key={n} className="skill-row off">
            <span className="skill-icon">
              <IconSparkles size={20} />
            </span>
            <div className="grow hint">正在上传并识别 {n}…</div>
          </div>
        ))}
        {skills !== null && personal.length === 0 && uploading.length === 0 ? (
          <div className="empty">
            <strong>还没有个人技能</strong>
            把你常用的做法写成 SKILL.md 上传，AI 就会按你的方式做事。不知道怎么写可以先下载模板。
          </div>
        ) : null}
        {personal.map((s) => (
          <SkillRow key={s.file_id} s={s} busy={busy === s.name} onToggle={(on) => toggle(s, on)} onDelete={() => remove(s)} />
        ))}
      </section>
      <p className="hint" style={{ marginTop: 14 }}>开关对你的账号生效，换浏览器也一样。个人技能的脚本只在你自己的沙箱里运行，沙箱不联网。</p>
    </main>
  );
}
