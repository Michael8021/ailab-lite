import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

// Run the dependency-free browser API module with Node's built-in test runner.
const source = await readFile(new URL("../lib/api.ts", import.meta.url), "utf8");
const compiled = ts.transpile(source, { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 });
const { api, streamRun, filesByIds, archiveUrl } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

test("a successful 204 DELETE has no JSON body", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 204 }));
  assert.equal(await api("/v1/conversations/c", { method: "DELETE" }), undefined);
});

test("SSE reader releases its lock on stream.end", async (t) => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("event: stream.end\ndata: {}\n\n"));
      controller.close();
    },
  });
  t.mock.method(globalThis, "fetch", async () => new Response(body));
  let ended = 0;
  await streamRun("r", () => {}, new AbortController().signal, () => ended++);
  assert.equal(ended, 1);
  assert.equal(body.locked, false);
});

test("filesByIds asks for all ids in one request, and none for an empty list", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url) => {
    calls.push(String(url));
    return new Response("[]", { status: 200 });
  });
  assert.deepEqual(await filesByIds([]), []);
  assert.equal(calls.length, 0);
  await filesByIds(["fil_a", "fil b"]);
  assert.equal(calls.length, 1);
  assert.match(calls[0], /\/v1\/files\?limit=200&ids=fil_a&ids=fil%20b$/);
});

test("archiveUrl repeats and encodes ids", () => {
  assert.equal(archiveUrl(["a", "b&c"]), "/api/v1/files/archive?ids=a&ids=b%26c");
});
