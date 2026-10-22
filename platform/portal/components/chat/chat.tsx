"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, Conversation, ConversationDetail, downloadUrl, ModelInfo, Preview, Run, streamRun } from "../../lib/api";
import { isTerminal } from "../../lib/format";
import { conversationsChanged } from "../app-shell";
import Composer, { SendOptions } from "./composer";
import PreviewPane from "./preview-pane";
import Turn, { Item, reduceEvent } from "./turn";

function useModels() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModelState] = useState("");
  useEffect(() => {
    // The platform decides which models exist; the user only picks one (their last choice is remembered).
    api<{ default: string | null; models: ModelInfo[] }>("/v1/models")
      .then((res) => {
        setModels(res.models);
        const remembered = localStorage.getItem("ailab.model");
        setModelState(res.models.some((m) => m.id === remembered) ? remembered! : res.default || res.models[0]?.id || "");
      })
      .catch(() => setModels([]));
  }, []);
  const setModel = (id: string) => {
    setModelState(id);
    localStorage.setItem("ailab.model", id);
  };
  return { models, model, setModel };
}

export default function Chat({ id }: { id: string | null }) {
  const router = useRouter();
  const { models, model, setModel } = useModels();
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [items, setItems] = useState<Record<string, Item[]>>({}); // run id -> live items (runs streamed in this page)
  const [livePreviews, setLivePreviews] = useState<Preview[]>([]);
  const [paneOpen, setPaneOpen] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [streaming, setStreaming] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const loadSeq = useRef(0);
  const streamController = useRef<AbortController | null>(null);

  const loadDetail = useCallback(async () => {
    if (!id) return null;
    const seq = ++loadSeq.current;
    try {
      const d = await api<ConversationDetail>(`/v1/conversations/${id}`);
      if (seq !== loadSeq.current) return null;
      setDetail(d);
      return d;
    } catch (err: any) {
      if (seq === loadSeq.current) setError(String(err?.message || err));
      return null;
    }
  }, [id]);

  const follow = useCallback(
    (run: Run) => {
      streamController.current?.abort();
      const controller = new AbortController();
      streamController.current = controller;
      setStreaming(run.id);
      setItems((prev) => ({ ...prev, [run.id]: prev[run.id] || [] }));
      streamRun(
        run.id,
        (event) => {
          if (controller.signal.aborted) return;
          setItems((prev) => ({ ...prev, [run.id]: reduceEvent(prev[run.id] || [], event) }));
          if (event.type === "preview.updated") {
            const p: Preview = { ...event.payload, run_id: run.id };
            setLivePreviews((prev) => [...prev.filter((x) => x.filename !== p.filename), p]);
            setSelected(p.filename);
            setPaneOpen(true);
          }
          if (["artifact.created", "run.completed", "run.failed", "run.canceled"].includes(event.type)) loadDetail();
        },
        controller.signal,
        () => {
          if (controller.signal.aborted) return;
          setStreaming(null);
          loadDetail();
          conversationsChanged();
        }
      ).catch((err) => {
        if (!controller.signal.aborted) {
          setStreaming(null);
          setError(String(err?.message || err));
          loadDetail();
        }
      });
      return controller;
    },
    [loadDetail]
  );

  // open a conversation: load it, and follow its last turn if that is still being answered
  useEffect(() => {
    if (!id) return;
    let disposed = false;
    let controller: AbortController | null = null;
    loadDetail().then((d) => {
      if (disposed) return;
      const last = d?.turns[d.turns.length - 1];
      if (last && !isTerminal(last.status)) controller = follow(last);
    });
    return () => {
      disposed = true;
      ++loadSeq.current;
      controller?.abort();
      streamController.current?.abort();
    };
  }, [id, loadDetail, follow]);

  // keep the newest message in view while it streams, unless the user scrolled up to read
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items, detail]);

  const previews = useMemo(() => {
    const byFile = new Map<string, Preview>();
    for (const p of [...(detail?.previews || []), ...livePreviews]) {
      byFile.delete(p.filename);
      byFile.set(p.filename, p);
    }
    return [...byFile.values()].filter((p) => p.kind);
  }, [detail, livePreviews]);

  const downloads = useMemo(() => {
    const out: Record<string, string> = {};
    for (const t of detail?.turns || []) for (const a of t.artifacts || []) out[a.filename] = downloadUrl(a.download_url);
    return out;
  }, [detail]);

  const modelName = (m: string) => models.find((x) => x.id === m)?.name || m;
  const ext = (name: string) => name.split(".").pop()?.toLowerCase() || "";
  // Previews are keyed by the file's name in the sandbox; deliver_file may have given it another name.
  // Then the one preview of the same kind that this turn rendered is the delivered file's.
  const previewKey = (run: Run, filename: string): string | null => {
    if (previews.some((p) => p.filename === filename)) return filename;
    const own = (run.result?.previews || []).filter((p) => ext(p.filename) === ext(filename));
    return own.length === 1 && previews.some((p) => p.filename === own[0].filename) ? own[0].filename : null;
  };
  const turns = detail?.turns || [];
  const running = turns.some((t) => !isTerminal(t.status));

  async function send(prompt: string, fileIds: string[], options: SendOptions = { mode: "work" }): Promise<boolean> {
    setBusy(true);
    setError("");
    try {
      let convId = id;
      if (!convId) convId = (await api<Conversation>("/v1/conversations", { method: "POST", body: JSON.stringify({ model }) })).id;
      const run = await api<Run>(`/v1/conversations/${convId}/messages`, {
        method: "POST",
        body: JSON.stringify({ prompt, model: model || undefined, file_ids: fileIds, ...options }),
      });
      if (!id) {
        conversationsChanged();
        router.push(`/c/${convId}`);
        return true;
      }
      setDetail((d) => (d ? { ...d, turns: [...d.turns, run] } : d));
      stick.current = true;
      follow(run);
      conversationsChanged();
      return true;
    } catch (err: any) {
      setError(String(err?.message || err).replace(/^409 .*/, "上一条消息还在回答中，请稍候或先停止它"));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    const active = turns.find((t) => !isTerminal(t.status));
    if (active) await api(`/v1/runs/${active.id}/cancel`, { method: "POST" }).catch(() => undefined);
    loadDetail();
  }

  const showPane = paneOpen && previews.length > 0;
  const composer = (
    <Composer
      models={models}
      model={model}
      onModel={setModel}
      busy={busy}
      running={running}
      onSend={send}
      onStop={stop}
      autoFocus
      placeholder={id ? "继续对话，比如「第三页换成图表」「再加一列占比」" : undefined}
    />
  );

  return (
    <div className={`chat-layout ${showPane ? "with-preview" : ""}`}>
      <section className="chat-main">
        {!id ? (
          <>
            <div className="chat-home">
              <h1>有什么我能帮你的吗？</h1>
            </div>
            <div className="composer-dock">
              {error ? <div className="alert error">{error}</div> : null}
              {composer}
            </div>
          </>
        ) : (
          <>
            <div
              className="messages"
              ref={scroller}
              onScroll={(e) => {
                const el = e.currentTarget;
                stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
              }}
            >
              <div className="messages-inner">
                {detail === null && !error ? <div className="hint">加载中…</div> : null}
                {turns.map((t) => (
                  <Turn
                    key={t.id}
                    run={t}
                    items={items[t.id]}
                    live={streaming === t.id}
                    modelName={modelName}
                    previewKey={previewKey}
                    onPreview={(f) => {
                      setSelected(f);
                      setPaneOpen(true);
                    }}
                  />
                ))}
              </div>
            </div>
            <div className="composer-dock">
              {error ? <div className="alert error">{error}</div> : null}
              {!paneOpen && previews.length ? (
                <button className="link" onClick={() => setPaneOpen(true)}>
                  打开预览（{previews.length}）
                </button>
              ) : null}
              {composer}
            </div>
          </>
        )}
      </section>

      {showPane ? (
        <PreviewPane
          previews={previews}
          selected={selected}
          onSelect={setSelected}
          onClose={() => setPaneOpen(false)}
          live={!!streaming && running}
          downloads={downloads}
        />
      ) : null}
    </div>
  );
}
