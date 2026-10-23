// The browser only ever talks to this Portal's own /api proxy (see app/api/v1). The access token
// is an httpOnly cookie that the proxy turns into an Authorization header on the server, so no
// token exists in this bundle, in URLs, or in anything JavaScript can read.
export const API_BASE = "/api";

/** A 401 means the session is gone or the token was rotated: back to the login page. */
export function redirectIfUnauthorized(resp: Response): void {
  if (resp.status === 401 && typeof window !== "undefined" && window.location.pathname !== "/login") {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
  }
}

export type Me = { id: string; username: string | null; display_name: string; email: string };

export type Run = {
  id: string;
  conversation_id?: string | null;
  title: string;
  prompt: string;
  status: string;
  model: string;
  summary: string;
  error: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  artifacts?: Artifact[];
  input_files?: { file_id: string; filename: string }[];
  options?: { mode?: "chat" | "work"; skill?: string; skill_title?: string };
  result?: { previews?: Preview[]; [key: string]: any };
  usage?: { prompt_tokens?: number; completion_tokens?: number; calls?: number };
};

/** A rendered preview of an office file a run produced (see the Broker's refresh_previews). */
export type Preview = {
  filename: string;
  version: number;
  run_id?: string;
  kind?: "pages" | "sheets";
  pages?: string[]; // "<n>/p-01.jpg", relative to the run's previews
  data?: string; // "<n>/sheets.json"
  errors?: string[];
  error?: string;
};

export type Skill = {
  name: string;
  title: string;
  description: string;
  builtin: boolean;
  enabled: boolean;
  scope: "builtin" | "personal";
  file_id?: string; // personal skills: the file in the library that defines it
  filename?: string;
  kind?: "md" | "zip";
  files?: number;
  error?: string | null; // why a personal skill file cannot be used
};

export type Conversation = {
  id: string;
  title: string;
  model: string;
  project_id?: string | null;
  created_at: string;
  updated_at: string;
  last_status?: string | null;
};

export type ConversationDetail = Conversation & { turns: Run[]; previews: Preview[] };

export function previewUrl(runId: string, name: string): string {
  return `${API_BASE}/v1/runs/${runId}/previews/${name}`;
}

export type Artifact = {
  id: string;
  file_id: string;
  filename: string;
  content_type: string;
  size: number;
  download_url: string;
};

export type ModelInfo = {
  id: string;
  name: string;
  description: string;
  weight: number;
  tools: boolean;
  default: boolean;
};

export type Limit = { used: number; limit: number };
export type Usage = { daily_tokens: Limit; monthly_tokens: Limit; library_bytes: Limit; resets_at_utc: string };

export type Info = {
  env: string;
  default_model: string | null;
  sandbox_provider: string;
  storage_backend: string;
  max_upload_bytes: number;
};

export type FileItem = {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  status: string;
  version: number;
  meta: Record<string, any>;
  created_at: string;
  download_url: string;
  project_id?: string | null;
  starred?: boolean;
  thumbnail_url?: string | null; // a small WebP the Worker made; absent for non-images and before processing
};

/** A project groups conversations and files; deleting it deletes both. */
export type Project = {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  conversations: number;
  files: number;
};

export type EventItem = {
  seq: number;
  type: string;
  payload: any;
  created_at?: string;
};

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string> | undefined) };
  if (init.body && typeof init.body === "string") headers["Content-Type"] = "application/json";
  const resp = await fetch(`${API_BASE}${path}`, { ...init, headers, cache: "no-store" });
  redirectIfUnauthorized(resp);
  if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export function downloadUrl(path: string): string {
  return `${API_BASE}${path}`; // same-origin: the session cookie authorizes the download
}

/** Several files by id in one request (unknown or foreign ids are left out). */
export function filesByIds(ids: string[]): Promise<FileItem[]> {
  if (!ids.length) return Promise.resolve([]);
  const query = ids.map((id) => `ids=${encodeURIComponent(id)}`).join("&");
  return api<FileItem[]>(`/v1/files?limit=200&${query}`);
}

/** One zip of several library files, streamed by the platform. */
export function archiveUrl(ids: string[]): string {
  return `${API_BASE}/v1/files/archive?${ids.map((id) => `ids=${encodeURIComponent(id)}`).join("&")}`;
}

/** Upload one file to the library (into a project when given). Throws with the server's reason. */
export async function uploadFile(file: File, projectId?: string | null): Promise<FileItem> {
  const form = new FormData();
  form.append("upload", file);
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  const resp = await fetch(`${API_BASE}/v1/files${query}`, { method: "POST", body: form });
  redirectIfUnauthorized(resp);
  if (!resp.ok) throw new Error(`${file.name}：上传失败（${resp.status}）`);
  return (await resp.json()) as FileItem;
}

export async function streamRun(
  runId: string,
  onEvent: (event: EventItem) => void,
  signal: AbortSignal,
  onEnd?: () => void
): Promise<void> {
  const resp = await fetch(`${API_BASE}/v1/runs/${runId}/events`, { signal });
  redirectIfUnauthorized(resp);
  if (!resp.ok || !resp.body) throw new Error(`event stream ${resp.status}`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx = buffer.indexOf("\n\n");
      while (idx >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const event = parseChunk(chunk);
        if (event) {
          if (event.type === "stream.end") {
            onEnd?.();
            return;
          }
          onEvent(event);
        }
        idx = buffer.indexOf("\n\n");
      }
    }
    onEnd?.();
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function parseChunk(chunk: string): EventItem | null {
  let type = "message";
  let id = 0;
  const data: string[] = [];
  for (const line of chunk.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("id:")) id = Number(line.slice(3).trim());
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!data.length) return null;
  try {
    return { seq: id, type, payload: JSON.parse(data.join("\n")) };
  } catch {
    return { seq: id, type, payload: { raw: data.join("\n") } };
  }
}
