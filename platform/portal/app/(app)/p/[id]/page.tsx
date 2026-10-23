"use client";

import { useParams } from "next/navigation";
import Chat from "../../../../components/chat/chat";
import { useProjects } from "../../../../lib/projects";

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const id = String(params?.id || "");
  const projects = useProjects();
  const project = projects?.find((p) => p.id === id);

  if (projects && !project) {
    return (
      <main className="page page-narrow">
        <div className="empty">
          <strong>找不到这个项目</strong>它可能已被删除。
        </div>
      </main>
    );
  }
  return project ? <Chat key={id} id={null} project={project} /> : null;
}
