"use client";

import { useEffect, useState } from "react";
import { api, Project } from "./api";

/** Pages tell each other (and the sidebar) that projects changed: created, renamed, deleted, files moved. */
export const PROJECTS_CHANGED = "ailab:projects";
export function projectsChanged(): void {
  window.dispatchEvent(new Event(PROJECTS_CHANGED));
}

// One list for the whole page: the sidebar, the chat crumb and the library all read it, one request serves them.
let cache: Project[] | null = null;
let inflight: Promise<void> | null = null;
let stale = false;
const listeners = new Set<(list: Project[]) => void>();

/** Fetch the list. `changed`: something was just modified, so an answer already on its way may predate it. */
function refresh(changed = false): Promise<void> {
  if (inflight) {
    if (changed) stale = true; // fetch once more when the current request returns
    return inflight;
  }
  inflight = api<Project[]>("/v1/projects")
    .then((list) => {
      cache = list;
      listeners.forEach((fn) => fn(list));
    })
    .catch(() => undefined)
    .finally(() => {
      inflight = null;
      if (stale) {
        stale = false;
        refresh();
      }
    });
  return inflight;
}

if (typeof window !== "undefined") window.addEventListener(PROJECTS_CHANGED, () => refresh(true));

export function useProjects(): Project[] | null {
  const [projects, setProjects] = useState<Project[] | null>(cache);
  useEffect(() => {
    listeners.add(setProjects);
    if (cache) setProjects(cache);
    else refresh(); // readers mounting together share the one request
    return () => {
      listeners.delete(setProjects);
    };
  }, []);
  return projects;
}

export async function createProject(name: string): Promise<Project> {
  const project = await api<Project>("/v1/projects", { method: "POST", body: JSON.stringify({ name }) });
  cache = [project, ...(cache || [])]; // a page opened for it right away must already find it
  listeners.forEach((fn) => fn(cache!));
  projectsChanged();
  return project;
}
