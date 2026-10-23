"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, FileItem, filesByIds } from "../../lib/api";

export type Category = "all" | "starred" | "images";
export type LibraryFilter = { category: Category; projectId?: string | null; q?: string };

const PAGE = 100;
const MAX = 200; // the API's largest page

function query(filter: LibraryFilter, limit: number, offset: number): string {
  const params = new URLSearchParams({ category: filter.category, limit: String(limit), offset: String(offset) });
  if (filter.projectId) params.set("project_id", filter.projectId);
  if (filter.q?.trim()) params.set("q", filter.q.trim());
  return `/v1/files?${params}`;
}

/** One filtered view of the library. Responses that arrive after the filter changed are dropped. */
export function useLibrary(filter: LibraryFilter, enabled = true) {
  const [files, setFiles] = useState<FileItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);
  const count = useRef(0);
  const key = `${filter.category}|${filter.projectId || ""}|${filter.q?.trim() || ""}`;

  // Refetch what is on screen (at least one page): after a change, and while scans are pending.
  const reload = useCallback(async () => {
    const mine = ++seq.current;
    const limit = Math.min(MAX, Math.max(PAGE, count.current));
    try {
      const list = await api<FileItem[]>(query(filter, limit, 0));
      if (mine !== seq.current) return;
      count.current = list.length;
      setFiles(list);
      setMore(list.length === limit);
      setError("");
    } catch (err: any) {
      if (mine === seq.current) setError(String(err?.message || err));
    } finally {
      if (mine === seq.current) setLoaded(true);
    }
  }, [key]);

  const loadMore = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const list = await api<FileItem[]>(query(filter, PAGE, count.current));
      if (mine !== seq.current) return;
      setFiles((prev) => {
        const seen = new Set(prev.map((f) => f.id));
        const next = [...prev, ...list.filter((f) => !seen.has(f.id))];
        count.current = next.length;
        return next;
      });
      setMore(list.length === PAGE);
    } catch (err: any) {
      if (mine === seq.current) setError(String(err?.message || err));
    }
  }, [key]);

  useEffect(() => {
    if (!enabled) return;
    count.current = 0;
    setLoaded(false);
    setFiles([]);
    reload();
    return () => {
      seq.current++;
    };
  }, [reload, enabled]);

  // Uploads stay "pending" until the Worker has scanned them. Ask about those alone, in one request.
  const pending = files
    .filter((f) => f.status === "pending")
    .map((f) => f.id)
    .join(",");
  useEffect(() => {
    if (!enabled || !pending) return;
    const view = seq.current;
    const timer = setInterval(async () => {
      const fresh: FileItem[] | null = await filesByIds(pending.split(",")).catch(() => null);
      if (!fresh || view !== seq.current) return; // the view changed meanwhile
      const byId = new Map(fresh.map((f) => [f.id, f] as const));
      setFiles((prev) => prev.filter((f) => f.status !== "pending" || byId.has(f.id)).map((f) => byId.get(f.id) || f)); // gone: deleted elsewhere
    }, 2000);
    return () => clearInterval(timer);
  }, [pending, enabled]);

  return { files, setFiles, loaded, more, error, reload, loadMore };
}
