import { afterEach, expect, it, vi } from "vitest";
import { chatRequest, readChatStream } from "../lib/chat-api";

afterEach(() => vi.unstubAllGlobals());
it("reads fragmented UTF-8 stream", async () => {
  const bytes = new TextEncoder().encode('{"type":"delta","text":"ответ"}\n{"type":"done"}\n');
  const stream = new ReadableStream({ start(c) {
    for (let i = 0; i < bytes.length; i += 3) c.enqueue(bytes.slice(i, i + 3)); c.close();
  } });
  const events: string[] = [];
  await readChatStream(new Response(stream), e => events.push(e.text ?? e.type));
  expect(events).toEqual(["ответ", "done"]);
});
it("rejects incomplete streams and safe errors", async () => {
  await expect(readChatStream(new Response('{"type":"delta","text":"partial"}\n'), () => {})).rejects.toThrow("прервано");
  await expect(readChatStream(new Response('{"type":"error","code":"timeout"}\n'), () => {})).rejects.toThrow("timeout");
});
it("does not treat HTTP 200 without done as a completed response", async () => {
  const events: string[] = [];
  const response = new Response(
    '{"type":"start"}\n{"type":"delta","text":"partial"}\n{"type":"error","code":"output_limit"}\n',
    { status: 200 },
  );
  await expect(readChatStream(response, event => events.push(event.type))).rejects.toThrow("output_limit");
  expect(events).toEqual(["start", "delta"]);
});
it("uses CSRF and passes cancellation signal", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ csrf_token: "synthetic" }))
    .mockResolvedValueOnce(new Response(""));
  vi.stubGlobal("fetch", fetcher);
  const abort = new AbortController();
  await chatRequest("/synthetic/messages", "POST", { content: "synthetic" }, abort.signal);
  expect(fetcher.mock.calls[1][1].headers["X-CSRF-Token"]).toBe("synthetic");
  expect(fetcher.mock.calls[1][1].signal).toBe(abort.signal);
});
