"use client";

import { useEffect, useState } from "react";
import { IconChart, IconSheet, IconSlides, IconSparkles } from "../../../components/icons";
import { api, Skill } from "../../../lib/api";

const ICONS: Record<string, (p: { size?: number }) => React.ReactElement> = { pptx: IconSlides, xlsx: IconSheet, data: IconChart };

function Switch({ on, label, onChange, busy }: { on: boolean; label: string; onChange: (on: boolean) => void; busy: boolean }) {
  return (
    <button
      role="switch"
      aria-checked={on}
      aria-label={label}
      className={`switch ${on ? "on" : ""}`}
      disabled={busy}
      onClick={() => onChange(!on)}
    >
      <span className="knob" />
    </button>
  );
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Skill[]>("/v1/skills")
      .then(setSkills)
      .catch((e) => setError(String(e?.message || e)));
  }, []);

  async function toggle(skill: Skill, enabled: boolean) {
    setBusy(skill.name);
    setError("");
    setSkills((list) => list?.map((s) => (s.name === skill.name ? { ...s, enabled } : s)) || list); // optimistic
    try {
      const saved = await api<Skill>(`/v1/skills/${skill.name}`, { method: "PUT", body: JSON.stringify({ enabled }) });
      setSkills((list) => list?.map((s) => (s.name === skill.name ? saved : s)) || list);
    } catch (e: any) {
      setSkills((list) => list?.map((s) => (s.name === skill.name ? { ...s, enabled: !enabled } : s)) || list);
      setError(`没能保存：${e?.message || e}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="page page-narrow">
      <div className="page-head">
        <h1>技能</h1>
        <p>技能是 AI 做特定工作时照着做的说明和工具。开启的技能会在需要时自动使用，也可以在输入框里直接点选；关闭后 AI 不会再用它，输入框里也不再显示。</p>
      </div>
      {error ? <div className="alert error" style={{ marginBottom: 12 }}>{error}</div> : null}
      <section className="card skill-list">
        {skills === null ? (
          <div className="empty">加载中…</div>
        ) : (
          skills.map((s) => {
            const Icon = ICONS[s.name] || IconSparkles;
            return (
              <div key={s.name} className={`skill-row ${s.enabled ? "" : "off"}`}>
                <span className="skill-icon">
                  <Icon size={20} />
                </span>
                <div className="grow">
                  <div className="skill-title">
                    {s.title} <span className="chip mono">{s.name}</span> {s.builtin ? <span className="chip">内置</span> : null}
                  </div>
                  <div className="skill-desc">{s.description}</div>
                </div>
                <Switch on={s.enabled} label={`${s.enabled ? "关闭" : "开启"} ${s.title}`} busy={busy === s.name} onChange={(on) => toggle(s, on)} />
              </div>
            );
          })
        )}
      </section>
      <p className="hint" style={{ marginTop: 14 }}>技能对你的账号生效，换浏览器也一样。上传自己的技能、管理员统一管理即将支持。</p>
    </main>
  );
}
