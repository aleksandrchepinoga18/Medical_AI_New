import { afterEach, expect, it, vi } from "vitest";
import { removeDocumentIndex, searchDocuments } from "../lib/rag-api";

afterEach(() => vi.unstubAllGlobals());

it("uses session CSRF for search without accepting a client owner", async () => {
  const fetch = vi.fn().mockResolvedValueOnce(Response.json({ csrf_token: "synthetic" }))
    .mockResolvedValueOnce(Response.json({ items: [], k: 5 }));
  vi.stubGlobal("fetch", fetch);
  await searchDocuments("synthetic query");
  const [path, options] = fetch.mock.calls[1];
  expect(path).toBe("/api/rag/search");
  expect(JSON.parse(options.body)).toEqual({ query: "synthetic query", k: 5 });
  expect(options.headers["X-CSRF-Token"]).toBe("synthetic");
  expect(options.method).toBe("POST");
  expect(options.cache).toBe("no-store");
});

it("does not remove an index when CSRF cannot be obtained", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response("private", { status: 403 }));
  vi.stubGlobal("fetch", fetch);
  await expect(removeDocumentIndex("synthetic")).rejects.toMatchObject({ status: 403 });
  expect(fetch).toHaveBeenCalledTimes(1);
});
